# Deep Agents 原生化重构方案

- 状态：Implemented（核心 Agent Harness）；Workspace/Sandbox 后端仍按路线图演进
- 基线日期：2026-08-02
- 目标：以 Deep Agents 作为模型驱动 Agent 的默认 Harness，以 LangGraph 作为唯一图运行时
- 当前事实：`deepagents==0.7.1` 已成为模型 Agent 的唯一 Harness；Capability、Skill、结构化输出、流式事件与原生 HITL 已接线

## 1. 文档目的

本文记录 MMAG 从“自研 LangGraph Agent Loop + 多个 Package Provider”迁移到“Deep Agents 原生 Harness + MMAG 企业治理”的实现决策，并保留后续 Workspace/Sandbox 演进边界。

本次重构解决四个问题：

1. 删除 `text-v1`、`json-v1`、`single-v1` 等把实现和版本泄漏到 Agent YAML 的 Provider；
2. 删除与 Deep Agents 重复的模型循环、工具循环、Skill 注入和上下文编排；
3. 给文件生产型 Agent 提供类似 Pi/Claude Code 的真实工作区与完整执行体验；
4. 保留 MMAG 已有的 Agent、Skill、Capability、Policy、审批、Artifact 和审计边界。

## 2. 核心决策

### 2.1 Deep Agents 的定位

Deep Agents 不是 LangGraph 之上的第二套 Runtime，也不是新的业务治理层。它是基于 LangGraph 的默认 Agent Harness：

```text
LangGraph
  └── Deep Agents Harness
        ├── Agent loop
        ├── Filesystem tools
        ├── Skills middleware
        ├── Subagent middleware
        ├── Summarization
        └── Human-in-the-loop
```

MMAG 不复刻这些能力，只通过 Deep Agents 的 model、tool、backend、middleware、checkpointer、interrupt 和 response format 扩展点接入企业能力。

### 2.2 Runtime 命名

Runtime 名称只表达行为，不携带实现版本：

```text
agent   模型驱动，默认由 Deep Agents 执行
direct  确定性地执行一个只读 Capability
```

`agent` 是默认值，因此普通 Agent Manifest 不必重复声明。版本继续由
`metadata.version`、Package Hash、Schema 版本和运行 provenance 表达。

以下名称全部删除，不保留兼容别名：

- `text-v1`；
- `json-v1`；
- `single-v1`；
- `deep-v1`；
- `json-v2` 等同类命名。

### 2.3 执行策略

当前阶段允许专用 Agent 在显式危险开关下使用本地完整 Shell，以尽快打通 Demo。接口必须从第一天与具体执行位置解耦：

```text
现在：GovernedWorkspaceBackend → LocalExecutionBackend
未来：GovernedWorkspaceBackend → RemoteSandboxBackend
```

本地 `cwd` 不是安全边界。本地完整 Shell 只能用于可信内部用户、专用测试主机或可重建虚拟机，不能
被描述为生产 Sandbox。

### 2.4 企业治理不下放给 Deep Agents

以下内容继续由 MMAG 作为单一事实来源：

- Agent、Skill、Policy、Model Policy、Capability 和 Execution Profile Registry；
- actor、scope、resource 与动态 Policy；
- Capability 执行授权、审批、预算和审计；
- Artifact 提交、交付、Outbox 和幂等；
- Package、Prompt、Schema、Skill 和执行环境 provenance。

Deep Agents 的文件权限、Skill 目录和原生审批只能作为运行机制，不能替代企业授权事实。

## 3. 当前真实状态

### 3.1 已实现基础

当前项目已经具备：

- `AgentRuntime`、`RunRequest`、`AgentResult` 公共契约；
- LangGraph SQLite checkpointer、稳定 `thread_id`、`interrupt()` 和恢复；
- Agent/Skill Package 严格 Loader、Registry、Schema、Hash 和 provenance；
- `CapabilitySpec`、`CapabilityRegistry`、`CapabilityExecutor` 与默认拒绝 Policy；
- Mattermost 流式文本、审批状态、Artifact 交付和 Outbox；
- `ExecutionProfile`、`ProcessRunner`、Run Workspace、Artifact staging 与清理；
- PPT 的可编辑 PPTX、源 Markdown 和 PNG 预览链路；
- 已删除的 Demo 专用 `ppt.shell` 宿主机高权限旁路。

### 3.2 已删除的重复层

迁移前的模型驱动链路为：

```text
AgentFactory
  → LangGraphTextProvider / LangGraphJSONProvider
  → RoutedModelRuntime / PackageAgentRunner
  → ModelGateway
  → LangGraphRuntimeAdapter
  → agent → review_tools → tools → agent
  → 自定义 Anthropic LLM client
```

以下重复职责已经删除：

- 手写 `StateGraph` Agent/Tool 循环；
- 自定义 Tool Call 解析与 Tool Result 回填；
- 文本 Provider 与 JSON Provider 分叉；
- 单 Skill 的 Prompt 拼接；
- 手工管理轮次耗尽与修复；
- Runtime 级模型路由包装；
- 后续若继续自研，还会重复 filesystem、todo、summarization 和 subagent。

### 3.3 当前实现边界

当前 `DeepAgentRuntime` 已完成显式 `ChatAnthropic`、Capability Tool 投影、StateBackend Skill 文件投影、
动态 HITL、SQLite checkpoint、跨进程审批恢复、结构化输出和流式文本事件。LangChain 原生模型/Tool
调用上限已按 Run 与 thread 强制；模型文件工具只允许读取 Skill 和读写受管 `/workspace/**`，其余路径
默认拒绝。首个 `GovernedWorkspaceBackend` 已将稳定 Run Workspace、canonical Capability、动态 Policy、
原生 HITL 和显式危险的本地 Demo Provider 接通。旧 Provider Registry、手写 Agent 图、自定义
Anthropic Client 和 Claude Agent SDK 并行链均已删除。

本地 Provider 不是 Sandbox：完整 Shell 只有设置 `MMAG_ALLOW_UNSAFE_LOCAL_EXEC=true` 才启用，使用
Execution Profile 限制超时和输出，并清空父进程环境。PPT 的 Artifact 自动 staging/commit、远程
Sandbox 与受信 Subagent 属于后续独立阶段。

## 4. 目标架构

```text
Mattermost / API
        │
        ▼
Application + AgentRouter
        │  选择 Agent Package
        ▼
ContractAgentDecorator
        │  输入 Schema / Prompt / SkillSet / Budget
        ▼
DeepAgentRuntime
        ├── ManagedChatModel
        ├── Capability Tools
        ├── StateBackend（当前 Skill 文件）
        ├── MMAG Skill Projection
        ├── PolicyReviewMiddleware
        ├── trusted Subagent specs
        └── LangGraph Checkpointer
                 │
                 ├── CapabilityExecutor
                 │     └── Policy / Approval / Audit
                 │
                 └── MMAG Execution Plane
                       ├── ProcessRunner（当前）
                       └── RemoteSandboxBackend（未来）
                                  │
                                  ▼
                       Artifact staging / Repository
                                  │
                                  ▼
                         Delivery / Mattermost
```

### 4.1 责任边界

| 组件 | 负责 | 不负责 |
|---|---|---|
| Deep Agents | Agent 循环、文件工具、Skill 披露、子任务、压缩 | 企业身份、授权、Artifact 发布 |
| LangGraph | checkpoint、interrupt、resume、图状态 | 业务审批资格、exactly-once 副作用 |
| Agent Package | 职责、路由、Prompt、Schema、预算、允许集合 | 创建 Tool、Secret、挂载和权限 |
| Skill Package | 工作方法、资源和能力需求 | 扩大 Agent 权限、直接执行脚本 |
| CapabilityExecutor | 输入校验、动态 Policy、审批、调用、审计 | Agent 规划和 UI 展示 |
| Workspace Backend | 文件与执行工具的统一语义 | 自行授予权限 |
| Execution Backend | 进程实际运行位置和生命周期 | Agent/Skill 决策 |
| ArtifactStore | 不可变产物、Hash、scope 和元数据 | Agent 临时工作记忆 |
| Mattermost UI | 流式展示、按钮和交付 | 决定业务授权 |

## 5. Agent Manifest 目标模型

### 5.1 模型驱动 Agent

普通 Agent 不再声明框架和 Provider。`runtime.mode` 缺省为 `agent`：

```yaml
metadata:
  name: ppt
  version: 3.0.0

spec:
  runtime:
    route: default
    max_turns: 12
    timeout_seconds: 900
    retry:
      max_attempts: 2

  skills:
    allow:
      - slides@3.0.0
    deny: []
    max_active: 3

  capabilities:
    allow:
      - workspace.read
      - workspace.write
      - workspace.execute
      - ppt.build
      - send_file
    deny: []

  execution_profiles:
    allow:
      - ppt@3.0.0
    deny: []
```

这里不存在 `provider: deep`。Deep Agents 是平台默认实现，不是 Package 可替换的任意代码入口。

### 5.2 确定性 Agent

确定性 Agent 显式使用 `direct`：

```yaml
spec:
  runtime:
    mode: direct
    capability: analyze_link
    source_argument: url
    timeout_seconds: 120
    retry:
      max_attempts: 2

  capabilities:
    allow: [analyze_link]
    deny: []
```

Loader 必须要求：

- `direct` 只能绑定一个真实 Capability；
- 该 Capability 必须等于 `capabilities.allow` 的唯一解析结果；
- 初期只允许 READ effect；
- `agent` 禁止出现 `capability` 和 `source_argument`；
- 默认 Agent 必须是 `agent` 模式。

### 5.3 Model Policy

模型供应商、模型 ID、temperature、最大输出和路由继续由 Model Policy 管理。Agent Manifest 只引用
`model_policy_ref`，不能直接填写模型端点、API Key 或 LangChain import。

## 6. DeepAgentRuntime

### 6.1 公共契约

`DeepAgentRuntime` 必须继续实现：

```python
class AgentRuntime(Protocol):
    async def run(self, request: RunRequest) -> AgentResult: ...
```

应用层、Router、control plane 和 Delivery 不依赖 Deep Agents 类型。Deep Agents 异常在 Runtime
边界翻译为现有稳定错误码。

### 6.2 图构建

每次 Run 按已验证 Package 快照构建：

```python
create_deep_agent(
    model=managed_chat_model,
    tools=capability_tools,
    system_prompt=rendered_system_prompt,
    backend=governed_workspace,
    skills=projected_skill_roots,
    subagents=trusted_subagents,
    interrupt_on=static_interrupt_rules,
    response_format=result_schema,
    checkpointer=shared_checkpointer,
    middleware=mmag_middleware,
    name=agent_name,
)
```

必须显式传入 model，不能依赖 Deep Agents 默认模型。`deepagents==0.7.1` 接收已初始化 Backend
实例，因此初期按 Run 创建 Backend 和图，避免跨 Run 共享可变工作区。后续只有在上游提供安全的
Backend factory 或本项目证明无状态后才能缓存整图。

### 6.3 Thread 与恢复

- `RunContext.run_id` 继续作为 LangGraph `thread_id`；
- 同一审批恢复必须使用原始 `thread_id`；
- Agent Package、SkillSet、Policy、Model Policy 和 Workspace snapshot 在首次运行时固定；
- resume 不重新路由 Agent，不读取最新 Package 覆盖旧 Run；
- interrupt 前不执行外部副作用；
- Capability 与执行命令使用稳定 execution key 防止节点重放导致重复写入。

### 6.4 结构化结果

模型驱动 Agent 统一使用 `response_format` 产生 Package 专属结构化结果，不再维护 text/json 两套
Provider。返回后仍执行：

1. Agent result JSON Schema 校验；
2. Skill output Schema 校验；
3. Artifact kind/scope/ref 校验；
4. 统一输出 Envelope 组装；
5. provenance 由平台注入。

`AgentResult` 应增加不可变 `output` 字段保存结构化业务结果；`text` 只保存用于展示的摘要。展示层
不得从 JSON 字符串重新解析控制状态。

## 7. 模型接入

当前由 `ManagedChatModelFactory` 显式初始化 `ChatAnthropic`，不再维护自定义 Anthropic client。
模型参数继续由 MMAG 的 Model Policy 和运行契约治理：

```text
ModelPolicyRegistry
  → ManagedChatModelFactory
  → ChatAnthropic(model, base_url, api_key, temperature, max_tokens)
  → usage / budget / audit middleware
```

要求：

- 复用当前 `ANTHROPIC_MODEL`、`ANTHROPIC_BASE_URL` 和 Secret Provider；
- Model Policy 真正驱动 model class、temperature 和最大输出；
- token、cost、latency、provider error 进入统一 usage；
- 不记录 Prompt、消息正文、thinking 或 Secret；
- Run 内不自动跨模型或 Runtime 重试；
- 删除 Runtime 包装式 `ModelGateway.run()`，将 Gateway 收敛为模型解析与预算治理，而不是第二层
  AgentRuntime 路由。

## 8. Capability 原生绑定

### 8.1 单一来源

Deep Agents 可见 Tool 必须由当前 Run 的 `CapabilitySpec` 生成：

```text
CapabilitySpec
  → LangChain StructuredTool schema
  → CapabilityExecutor.invoke()
  → Policy / Approval / Handler / Audit
```

Tool adapter 只负责：

- JSON Schema 到 Tool 参数 Schema；
- 当前 RunContext 注入；
- 结构化结果转换；
- 稳定错误语义。

它不能复制 Policy、审批、预算或 handler。

### 8.2 Workspace Tool 映射

Deep Agents 内建工具必须映射到平台注册 Capability：

| Deep Agents Tool | MMAG Capability |
|---|---|
| `ls`、`glob`、`grep`、`read_file` | `workspace.read` |
| `write_file`、`edit_file` | `workspace.write` |
| `execute` | `workspace.execute` |

`GovernedWorkspaceBackend` 的每个方法都必须调用相应 CapabilityExecutor。`workspace.execute` 的
handler 调用原始 `ExecutionBackend`，不能回调 Backend 自己造成递归。

### 8.3 权限交集

一次调用的有效权限为：

```text
Agent allowlist
∩ 候选 SkillSet 的能力并集
∩ 当前动态 Policy
∩ Execution Profile
∩ Workspace 生命周期状态
```

未知 Capability、缺失 actor/scope、未绑定 Profile、Workspace 已关闭或 Policy 无匹配规则均默认拒绝。

## 9. Workspace 与执行 Backend

### 9.1 StateBackend 的使用边界

`StateBackend` 只用于小型文本工作记忆，例如计划、摘要和少量 JSON。禁止存储 PPTX、PDF、图片、
数据集和大型日志，避免 checkpoint 膨胀。

文件生产型 Agent 使用真实 Run Workspace：

```text
runs/<run-id>/
  workspace/   # Agent 可见工作目录
  input/       # 经过校验的输入 Artifact
  staging/     # 唯一正式产物出口
  logs/        # 截断后的完整命令输出
  meta.json    # 平台维护，模型不可写
```

### 9.2 Backend 协议

业务层只依赖：

```python
class ExecutionBackend(Protocol):
    async def create(self, request: ExecutionRequest) -> ExecutionSession: ...
    async def execute(self, session, command, timeout) -> ExecutionResult: ...
    async def upload(self, session, files) -> None: ...
    async def download(self, session, paths) -> tuple[DownloadedFile, ...]: ...
    async def destroy(self, session) -> None: ...
```

`ExecutionSession` 保存 backend ID、Run ID、生命周期状态和 provenance。Agent YAML 只引用
Execution Profile，不选择 endpoint、credential 或宿主挂载。

### 9.3 LocalExecutionBackend

第一阶段只实现本地 Backend。它允许完整 Bash/Python/Node，但必须满足最低控制：

- 显式 `MMAG_ALLOW_UNSAFE_LOCAL_EXEC=true` 才能启动；
- 启动日志和 Run provenance 标记 `unsafe-local`；
- 使用 `asyncio.create_subprocess_exec`，不使用 Python `shell=True`；
- 每个 Run 独立目录和进程组；
- 明确的 timeout、输出、文件大小和并发限制；
- 父进程环境默认清空，只注入白名单；
- Mattermost、模型、MCP、数据库等 Secret 不进入进程；
- stdout/stderr 流式输出、截断并写入受管日志；
- 命令、退出码、耗时和资源消耗进入审计；
- 完成、失败、取消和 TTL 到期后回收工作区。

即使设置了 `cwd`，Shell 仍能访问宿主其他路径和网络。该模式不能用于公网、多租户或含敏感数据的
企业生产环境。

### 9.4 RemoteSandboxBackend

后续 Remote Backend 复用同一协议，至少提供：

- 每 Run 或每 thread 的隔离实例；
- 非 root、只读根文件系统和受控可写目录；
- CPU、内存、磁盘、进程数和 wall-clock 限制；
- 默认断网和按策略放行；
- Secret 留在控制面或短期最小注入；
- Artifact upload/download 与 Hash 校验；
- create/execute/download/destroy 幂等；
- 异常回收、TTL 和成本审计。

远程 Provider 是部署配置，不进入 Agent Package。引入远程实现时只增加一个 Backend，不重写
Agent、Skill 或 DeepAgentRuntime。

## 10. Skill 原生集成

### 10.1 MMAG Registry 仍是源事实

Deep Agents SkillsMiddleware 只负责发现和渐进披露。Skill 的版本、Hash、Schema、Capability 需求、
资源预算和 provenance 仍来自 MMAG Skill Registry。

每次 Run 将允许的 Skill 投影到生成目录：

```text
runtime-skills/
  slides/
    SKILL.md
    references/...
    templates/...
```

投影规则：

- 只投影已解析精确版本的 Skill；
- 同名 Skill 在投影前直接失败，不能采用“最后来源覆盖”；
- `scripts/` 不进入 SkillsMiddleware；
- reference/template 保持只读并校验 Hash；
- Registry 在投影前校验 ref、Hash、UTF-8 与声明预算；StateBackend 只暴露本次选中的 Package；
- Skill 修改不能反写源码 Package。

### 10.2 单次选择、原生披露

当前 `SkillResolver` 在 Agent 白名单内选择一个 Skill，并把已验证的 Skill Package 投影到
Deep Agents StateBackend 的 `/skills/<name>/`。Deep Agents SkillsMiddleware 负责原生渐进披露：

```text
Agent allowlist
  → intent/requested_skill 选择一个 Skill
  → Capability 交集
  → StateBackend 投影
  → SkillsMiddleware 按需读取 SKILL.md/资源
  → provenance 记录选中 Skill
```

显式 requested skill 仍必须属于 Agent allowlist。有效 Tool 集合按 Agent 与选中 Skill 的能力交集计算，
Skill 不能扩权。以后只有出现真实的并行多 Skill 需求时才扩展为有界 SkillSet，不预先增加 Manifest 字段。

旧 `load_skill_resource` 已删除，不保留两套
资源加载路径。

## 11. Subagent

Deep Agents 默认可能加入通用 Subagent，并让 Subagent 继承主 Agent Tool。企业环境初期必须：

- 禁用默认 `general-purpose` Subagent；
- 不向尚未声明 Subagent 的 Agent 暴露 `task`；
- 只从受信 Agent Registry 生成 Subagent spec；
- 子 Agent 权限取父 Agent、子 Agent Manifest 和当前 Policy 的交集；
- 子 Agent 自有 permissions、tools、skills 不能覆盖并扩大父级权限；
- 子 Agent 使用独立预算、usage 和审计关联；
- 第一阶段不启用异步远程 Subagent。

在单 Agent Runtime、Skill、HITL 和 Workspace 稳定前，所有 Agent 的 Subagent allowlist 为空。

## 12. 人在回路

### 12.1 两类审批

静态高风险 Tool 可以由 Deep Agents `interrupt_on` 声明；资源级动态决策仍由 MMAG
`PolicyReviewMiddleware` 完成：

```text
Tool call
  → 参数 Schema
  → Policy preview（纯函数）
  ├── deny             → 结构化拒绝
  ├── allow            → CapabilityExecutor
  └── require_approval → LangGraph interrupt
                             ↓
                       Mattermost 按钮
                             ↓
                       Approval Grant
                             ↓
                       CapabilityExecutor 再鉴权
```

### 12.2 恢复规则

- approve、reject、respond 可以映射到 Deep Agents 原生动作；
- WRITE、Shell 和外发工具初期禁用任意 edit；
- 如果以后允许 edit，修改后的 tool name/args 必须重新执行 Schema、Policy 和审批判断；
- Approval Grant 绑定 actor、scope、Package/Skill snapshot、tool、args hash、过期时间和 execution key；
- Mattermost 回调只能提交用户选择，不能自行构造授权事实；
- 无 UI 场景默认拒绝需要审批的调用。

## 13. 流式输出与 Mattermost

`RunEvent` 从单一 `text_delta` 扩展为受控事件：

```text
text.delta
tool.started
tool.progress
tool.completed
approval.required
artifact.created
run.status
```

Deep Agents 使用 LangGraph `astream` 的 messages/updates 事件，Runtime adapter 转换成上述公共事件。
要求：

- 不流式展示隐藏 reasoning；
- Tool 参数先脱敏再展示；
- 高频 delta 继续由现有 Mattermost 节流器合并；
- Tool/审批/Artifact 使用结构化 UI，不混进最终自然语言；
- 断线重连后以 Run 状态和 checkpoint 为准，不重复发送已确认的 Artifact；
- 最终 `AgentResult` 是权威结果，流式事件只是过程展示。

## 14. Artifact 与 PPT 闭环

PPT Agent 的目标流程为：

```text
用户请求
→ Router 选择 ppt
→ Deep Agents 读取 slides Skill
→ workspace 写入 slides.md / theme / assets
→ 调用 workspace.execute 或高层 ppt.build
→ staging 产生 PPTX / PDF（可选）/ PNG preview
→ ArtifactRepository 校验并提交
→ Policy/Approval
→ Delivery/Outbox 上传 Mattermost
→ Workspace 回收
```

Demo 阶段可以保留 `ppt.build` 高层 Capability，同时开放 `workspace.execute` 处理构建调试。完成通用
Workspace 接入后已删除专用 `ppt.shell`，同一 Agent 不再存在两个 Shell 入口。

Agent 返回的只能是 Artifact ref，不能返回本地绝对路径。Artifact provenance 至少包含 Agent、Skill、
Prompt、Schema、Policy、Model Policy、Deep Agents 版本、Backend、Execution Profile、命令 Hash 和输入
输出 Hash。

## 15. 预算、日志与审计

### 15.1 预算

一次 Run 同时限制模型调用/token/成本、Tool 调用、Workspace 读写、Skill 披露、execute 次数与时长、
Artifact 数量与大小，以及启用后的 Subagent 数量与并发。检查必须发生在调用前且记录原子化；
LangGraph resume 不能重新获得一份新预算。

### 15.2 结构化日志

统一关联 `trace_id/run_id/thread_id/task_id`、actor/scope、Agent/Skill、Capability/Tool、Approval、
Execution、Artifact 和 Delivery。普通日志不记录 Prompt、消息正文、thinking、完整 Tool 参数、
stdout/stderr 或 Secret。

### 15.3 审计事件

至少记录 Agent Run 启停/失败/暂停/恢复、Skill 披露、Capability 决策与结果、Approval、Workspace、
Execution、Artifact 提交以及 Delivery 状态事件。

## 16. 目标代码结构

保持模块化单体，不创建庞大的 `deep/` 子系统：

```text
src/mmag/
  runtimes/{base,deepagents,direct}.py
  agent_packages/{factory,runtime}.py
  capabilities/{bindings,workspace}.py
  execution/{backend,local,workspace,process}.py
  skill_packages/projection.py
```

`deepagents.py` 负责 Runtime 与事件/结果适配；`direct.py` 负责确定性 Capability；`backend.py` 和
`local.py` 分离协议与本地实现；`projection.py` 只做可信 Skill 投影。

单文件不因超过 300 行拆分；只有超过约 800 行且职责已经分化时才拆。

## 17. 删除清单

达到功能等价后一次删除旧链路，不保留兼容框架：

- `LangGraphTextProvider`；
- `LangGraphJSONProvider`；
- `RoutedModelRuntime`；
- `PackageAgentRunner` 内自定义 JSON 模型循环；
- 手写 `agent → review_tools → tools` 图；
- `LangGraphState` 与自定义 Anthropic Tool Call 格式；
- `text-v1/json-v1/single-v1` Provider 注册；
- `USE_SDK_LLM`、`ClaudeSDKRuntimeAdapter` 和并行 Claude SDK Agent 路径；
- `load_skill_resource`；
- 通用 Workspace 就绪后删除 `ppt.shell`；
- 仅为旧 Provider 存在的配置、测试和文档。

保留：

- `AgentRuntime`、`RunRequest`、`AgentResult`；
- LangGraph checkpointer；
- Agent/Skill/Policy/Model Policy/Capability/Execution Profile Registry；
- `CapabilityExecutor`；
- control plane、Approval、Artifact、Outbox 和 Delivery。

## 18. 分阶段迁移

### 阶段 A：契约收敛（已完成）

1. 修订 Agent Manifest Schema：删除 `spec.execution.kind/provider`；
2. 将运行方式合并到 `spec.runtime.mode`，默认 `agent`；
3. `AgentResult` 增加结构化 `output`；
4. 扩展 `RunEvent`；
5. 建立 Model Policy → `BaseChatModel` 工厂；
6. 更新全部 Agent YAML 并提升版本。

退出标准：Package 可以只用 `agent/direct` 表达运行方式，旧 Provider 名称不再出现在源码和测试中。

### 阶段 B：Deep Agents 最小闭环（已完成）

1. 实现 `DeepAgentRuntime`；
2. 复用 SQLite checkpointer 和稳定 thread ID；
3. 将 CapabilitySpec 绑定成 LangChain Tool；
4. 禁用默认通用 Subagent；
5. 使用 StateBackend 跑通 `mmchat`；
6. 使用原生 `response_format` 跑通结构化 Agent。

退出标准：mmchat/report 在 Deep Agents 上完成文本、Tool、结构化输出和 usage，不再进入手写图。

### 阶段 C：Skill 原生化（已完成单 Skill 闭环）

1. 实现可信 Skill 投影；
2. `SkillResolver` 保持确定性的单 Skill 选择；
3. 接入 SkillsMiddleware；
4. 记录选中 Skill 与完整投影 Hash；
5. 删除旧 Prompt 拼接和 `load_skill_resource`。

退出标准：一个 Run 只投影一个允许 Skill，SKILL.md/资源由原生 middleware 渐进读取，Skill 不能扩权。

### 阶段 D：Workspace 与本地完整执行（后续）

1. 实现 `GovernedWorkspaceBackend`；
2. 注册 `workspace.read/write/execute`；
3. 实现显式危险的 `LocalExecutionBackend`；
4. 复用 Workspace、ProcessRunner、Artifact staging 和回收；
5. 迁移 PPT Agent；
6. 删除 `ppt.shell`。（已完成）

退出标准：PPT Agent 在真实 Run Workspace 中创建源文件、执行构建、提交 Artifact 并交付 Mattermost；
所有执行都可审计，关闭危险开关时失败关闭。

### 阶段 E：HITL 与交互层（Runtime 已完成，交互细节继续优化）

1. 实现动态 `PolicyReviewMiddleware`；
2. 对接 Deep Agents 原生 interrupt；
3. 改造 ApprovalCoordinator 适配通用 LangGraph resume；
4. Mattermost 提供 approve/reject/respond 按钮；
5. 接入 Tool、Approval、Artifact 流式事件。

退出标准：高风险 Tool 在副作用前暂停，选择按钮后以同一 thread 恢复，未经授权不能执行。

### 阶段 F：删除旧 Runtime（已完成）

1. 全部模型驱动 Agent 迁移；
2. link 切换为 `direct`；
3. 删除旧 Provider、自研图、Claude SDK 可选链和兼容配置；
4. 更新 README、Package、Skill、Execution、Operations 和 Roadmap；
5. 只保留直接相关回归与安全负向测试。

退出标准：仓库只有 Deep Agents 模型路径和 direct 确定性路径，不存在运行时双轨。

### 阶段 G：远程 Sandbox（有预算后）

1. 选择一个生产 Provider；
2. 实现 `RemoteSandboxBackend`；
3. 完成 upload/download、网络、Secret、配额、TTL 和异常回收；
4. 把部署配置从 `local` 切换到 `remote`；
5. 完成越权、泄漏、重放和故障恢复验收。

退出标准：不修改 Agent/Skill/Runtime 即可把同一任务切换到远程隔离环境。

## 19. 验证策略

用户要求先完成整体，因此每个阶段只运行最小相关验证，不在早期堆叠大量测试。

必须保留的门禁包括 Manifest 新旧字段和原子加载、CapabilityExecutor 单入口、默认拒绝、稳定
thread ID 与副作用幂等、Workspace 穿越/符号链接、Secret 隔离、危险开关失败关闭、Artifact staging、
默认 Subagent 关闭和旧 Provider 名称清除。

不在默认测试中调用真实 LLM、Mattermost、公网、远程 Sandbox 或 LibreOffice。

## 20. 发布、回滚与运行门禁

重构不使用长期兼容层。迁移按完整业务 Agent 切片完成，每个阶段合并前保持主分支可运行。

发布门禁：

- `deepagents`、LangGraph、模型 Provider 和 Schema 版本进入构建清单；
- Package Hash、graph fingerprint 和 Backend 类型进入 Run provenance；
- 本地高权限模式必须在部署配置中显式开启；
- 生产环境若检测到 `unsafe-local` 必须拒绝启动或输出阻断级告警；
- checkpoint 与 control plane 数据升级前备份；
- 产生副作用后不自动回退到旧 Runtime。

回滚方式是回滚整个应用版本和 Manifest 快照，而不是在同一个 Run 中切换 Runtime。处于审批暂停的
旧 Run 必须由兼容的应用版本完成或明确取消，不能用新图强行恢复旧 checkpoint。

## 21. 验收标准

重构完成必须同时满足：

1. Deep Agents 是所有模型驱动 Agent 的唯一 Harness；
2. LangGraph 仍是唯一 checkpoint/HITL Runtime；
3. Agent YAML 不包含实现 Provider 和 `-v1` Runtime 名称；
4. direct Agent 不调用模型；
5. Capability、Skill、Policy 和 Execution Profile 不能自授权；
6. 一个 Run 只投影 Router 已选择且通过能力交集的 Skill；
7. 默认通用 Subagent 被禁用；
8. PPT 可在真实 Workspace 完成源文件、构建、Artifact 和 Mattermost 交付；
9. 本地完整 Shell 可通过一个显式开关启停，并明确标记为非 Sandbox；
10. 未来切换远程 Sandbox 不修改 Agent/Skill Manifest；
11. 审批、预算、日志、审计和 provenance 覆盖完整链路；
12. 旧 Provider、自研 Agent Loop、Claude SDK 并行链和 `ppt.shell` 均已删除；本地 Demo Shell 统一由可替换 Workspace Backend 承载。

## 22. 明确不做

- 不把 Deep Agents 做成第二套 Agent/Skill/Policy Registry；
- 不新增 `execution.kind=deepagent`；
- 不保留 `*-v1` 兼容别名；
- 不直接使用 Deep Agents `LocalShellBackend`；
- 不把 `StateBackend` 当作 ArtifactStore；
- 不允许 Agent Manifest 填写 Sandbox endpoint、credential、挂载或网络规则；
- 不让 Subagent 自己覆盖父 Agent 权限；
- 不把 Prompt 中的“请勿越权”当作安全边界；
- 不为未来多 Provider 提前实现复杂 Sandbox Registry；
- 不在本次重构中拆分微服务。

## 23. 最终结论

目标不是“把 Deep Agents 加进来”，而是用它删除 MMAG 重复的 Agent Harness，同时把企业治理能力
注入它的原生扩展点：

```text
Deep Agents 决定怎样完成任务
MMAG 决定它能看见什么、能执行什么、谁能批准、产物如何交付
ExecutionBackend 决定命令在哪里运行
LangGraph 保证任务可以暂停、恢复和追踪
```

先使用显式不安全的本地 Backend 打通真实工作区和完整 Shell，后续只替换为远程 Sandbox。该路径
兼顾 Demo 速度、代码收敛和企业演进，不需要维护两套 Agent Runtime。
