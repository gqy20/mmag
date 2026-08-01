# mmag AI Native 协同架构重构方案

> 状态：Active / 分阶段实施中
> 基线日期：2026-07-31  
> 适用版本：mmag 0.1.x（当前 `main`）  
> 输入依据：当前代码、现有 Roadmap/Tech Debt、`ai_native.png` 架构图

![企业 AI Native 协同架构](../ai_native.png)

### 实施进度

截至 2026-07-31，Phase 0 已完成第一批测试护栏和数据演进基础：

- 默认 pytest 不再收集 `tests/poc`；
- 真实 Mattermost、LLM 和公网测试统一标记为 `external`，默认不执行；
- 新增离线消息主链契约测试，覆盖消息持久化、显式路由、Runtime 和回复投递；
- 修复 Runtime 失败提示被发送层吞掉的问题；
- 测试数据库改用内存 SQLite；
- 已建立版本化 SQLite migration，覆盖新库初始化、旧字段补齐、旧消息/FTS 迁移、幂等、失败回滚和未来版本拒绝；
- `Memory` 不再负责建表和历史 schema 升级；业务消息与 FTS 写入也具备失败回滚；
- Secret 日志、文件路径和 MCP 工具已经改为显式安全边界；
- GitHub Actions 与本地统一执行 `make verify`，当前基线为 `255 passed, 2 deselected`、59.15% 分支覆盖率、52 个源码文件 mypy 零错误；
- `uv.lock` 已提交，默认 Prompt 已作为 wheel 资源发布并通过隔离 smoke test。
- 重复 posted 事件已在执行前去重，Mattermost 回复已具备带 `pending_post_id` 的安全有界重试。

Phase 0、Runtime 契约与 Capability 单一来源均已完成：`Agent`、`MemoryCompactor` 只依赖统一 Runtime Port；八个内置能力与外部 MCP 都经过 Capability Spec、Executor 和 Policy。LangGraph 已成为默认 Runtime，并使用 SQLite checkpoint、稳定 `thread_id`、原生 `interrupt()` / `Command(resume=...)` 实现副作用前人工审批；Claude Agent SDK 保留为显式可选后端。

下一步不直接拆分 `Agent` 或引入多 Agent，而是按以下依赖顺序推进：

| 顺序 | 实施包 | 核心产出 | 为什么先做 |
|---|---|---|---|
| 1 | 收口 Phase 0 工程门禁（完成） | CI、coverage 基线、类型检查基线、wheel smoke test | 让后续每次重构都有自动回归门禁 |
| 2 | Runtime 契约（完成） | `RunRequest`、`AgentResult`、`AgentRuntime` 与统一错误模型 | 先稳定调用边界，再替换内部实现 |
| 3 | Capability 单一来源（完成） | `CapabilitySpec`、执行器、内置与 MCP 的双 Runtime binding | 用垂直切片验证工具不再重复定义 |
| 4 | 入口与执行解耦 | `InboundEvent`、按会话分区的执行队列、Outbox | 在统一执行协议后再引入并发和恢复 |

可执行任务、边界和验收标准以 [`ROADMAP.md`](./ROADMAP.md) 为准；本文档保留目标架构和设计理由。

## 1. 文档目的

本文档用于回答三个问题：

1. mmag 当前真实架构是什么，已经具备哪些能力；
2. 若以“企业 AI Native 协同架构”为目标，系统缺少哪些关键边界；
3. 如何在保持现有 Mattermost Bot 可用的前提下，分阶段重构成支持多人、多 Agent、企业上下文和私有化治理的协同平台。

本文档是架构方向和迁移计划，不代表其中的目标模块已经实现。

### 1.1 本次重构的范围

- Mattermost 协作入口解耦；
- 消息接收、任务执行与结果投递解耦；
- LLM Runtime 和工具系统统一；
- 企业上下文、任务状态与交付物建模；
- Managed Agent 注册、路由和协作协议；
- 权限、审计、模型访问和私有化治理边界；
- 支撑以上变化的测试、迁移和可观测性基础。

### 1.2 暂不包含

- 立即拆成微服务；
- 一次性实现 PPT、研报、项目助理等全部数字员工；
- 一次性替换 SQLite 或 Mattermost；
- 大规模前端工作台建设；
- 在缺少契约测试前进行破坏性重写。

## 2. 执行摘要

当前 mmag 已具备 Mattermost 实时接入、上下文窗口、持久化消息、用户画像、团队知识、摘要、多模态、工具调用、MCP 和两种 LLM Runtime。它是一个功能较丰富的单 Bot Agent。

但当前系统还不是“AI 协同平台”：

- `Agent` 同时承担入口、应用编排、上下文、策略、执行和输出，是系统的事实中心；
- Claude Agent SDK 与 Legacy LangGraph 是两条不同的运行链路；
- 内置工具在两套 Runtime 中重复定义，已经出现行为漂移；
- WebSocket 事件处理串行等待完整 LLM/工具链，无法安全支撑多人并发；
- Memory 以频道消息为核心，尚未建立组织、项目、客户、任务、决策、Agent Run、交付物和审计等企业上下文；
- 当前“链接分析”等能力是工具，不是可独立配置、授权、审计和路由的 Managed Agent。

因此本次重构的首要目标不是增加更多 Agent，而是先建立统一的控制面：

```text
统一事件 → 会话协调 → 上下文解析 → 任务路由 → Agent 执行 → 结果汇总 → 审计投递
```

推荐顺序：

1. 建立关键链路契约测试和数据库迁移机制；
2. 统一 Runtime 与 Capability 协议；
3. 将消息接收和任务执行解耦；
4. 建立企业上下文与任务/产物模型；
5. 引入 Agent Registry 和 Router；
6. 最后完善治理与私有化部署能力。

## 3. 对 AI Native 架构图的理解

架构图表达的重点不是“系统中存在很多 Agent”，而是以下四层形成闭环。

| 架构层 | 核心职责 | 对 mmag 的含义 |
|---|---|---|
| 协作入口 | 群组会话、多模态工作台、个人会话 | Mattermost 是第一个 Adapter，未来可增加 Web/API，不应侵入业务内核 |
| AI 协同中枢 | 上下文继承、意图识别、任务拆解、权限、文件、结果汇总 | 应成为稳定控制面，而不是负责所有专业工作的超级 Agent |
| Managed Agents | 链接、PPT、研报、项目助理等数字员工 | 每个 Agent 应声明能力、输入输出、权限、预算、作用域和运行状态 |
| 企业上下文与治理 | 人员、项目、客户、文档、决策、任务、知识、产物、模型和审计 | 上下文必须有明确作用域和访问策略，不能只依赖频道消息与 Prompt |

“专属数字员工”可理解为一个 Agent Profile 与用户、会话或项目的绑定；“受管数字员工”则是平台注册、路由、授权和审计的 Agent 实例。

## 4. 当前真实架构

### 4.1 当前运行链路

```mermaid
flowchart LR
    MM[Mattermost] -->|WebSocket event| WS[WebSocketClient]
    WS --> IN[(Inbox / Scheduler)]
    IN --> APP[application.MessageHandler]
    APP --> ROUTER[agent_system.AgentRouter]
    ROUTER --> PKG[Agent Package Contract]
    PKG --> LG[LangGraph default Runtime]
    LG --> REVIEW{review_tools}
    REVIEW -->|REQUIRE_APPROVAL| CP[(SQLite checkpoint)]
    CP -->|Command resume| REVIEW
    REVIEW --> REG[CapabilityRegistry / MCP Bridge]
    PKG -. explicit opt-in .-> SDK[Claude Agent SDK]
    SDK --> SDKTOOLS[SDK Tools / in-process MCP]
    SDKTOOLS --> OUT[(Outbox)]
    REG --> OUT
    LG --> OUT
    OUT -->|REST delivery| MM
```

入口在 [`cli.py`](../src/mmag/cli.py)，依赖由 [`application/app.py`](../src/mmag/application/app.py) 组合；消息、上下文、附件和投递分别由应用服务承担。旧的单文件 `agent.py` 已删除。

### 4.2 当前已有能力

| 能力 | 当前实现 | 评价 |
|---|---|---|
| Mattermost 接入 | `ws_client.py` + `client.py` | 已可用，但协议层和业务执行未隔离 |
| 显式召唤 | @、DM、thread 硬规则 | 可保留为中枢路由规则 |
| 群聊自主响应 | LLM 输出 `<SILENT>` | 可用，但故障和沉默语义混在一起 |
| 上下文窗口 | `working_memory` + `_build_context` | 以频道为中心，耦合 MM/Memory/Prompt |
| 长期记忆 | SQLite 消息、FTS、画像、知识、摘要 | Repository 已拆分，Scope/Artifact 主链仍需收口 |
| 多模态 | 图片、文本附件转 content blocks | 已具备入口侧能力 |
| LangGraph Runtime | Anthropic API + LangGraph | 当前默认路径，具备持久 checkpoint 与原生 HITL |
| SDK Agent Loop | Claude Agent SDK | 显式 opt-in 路径，不承担可恢复审批 |
| 内置能力 | 消息、知识、链接、用户、文件等 | 单一 Spec，经 CapabilityRegistry/SDK 投影 |
| MCP | Capability Catalog + MCP Bridge | schema、策略和可见性已有单一来源 |
| 日志追踪 | ContextVar `TraceContext` | 请求隔离，已记录 route/package/runtime |

### 4.3 主要架构问题

#### A. `Agent` God Object（已解决）

旧 `agent.py` 曾超过 1200 行并同时承担：

- 依赖构造和生命周期；
- Mattermost 事件过滤与解析；
- 历史 backfill；
- 附件下载与内容转换；
- 消息持久化与用户画像；
- 触发判定；
- System Prompt 与上下文构建；
- LLM Runtime 选择；
- typing、ack、回复与错误降级。

该文件已经删除。当前由 `application/app.py` 负责组合，消息、上下文/附件和 Delivery 使用独立应用服务；拆分依据是职责和所有权，不是 300 行阈值。

#### B. Runtime 与工具系统双轨（已解决）

- [`llm.py`](../src/mmag/llm.py) 只封装模型调用，[`runtimes/langgraph.py`](../src/mmag/runtimes/langgraph.py) 统一图编排；
- [`sdk_llm.py`](../src/mmag/sdk_llm.py) 使用 Claude Agent SDK，但不再模拟旧手写 Agent loop 参数；
- 内置能力由 `capabilities/` 的唯一 Spec 创建，LangGraph 与 SDK 只负责投影 binding；
- 参数上限、结果格式化、授权和来源增强由 Capability 层统一；
- MCP 先进入 Capability Catalog，再生成 Runtime binding；SDK 仅作为可选后端。

LangGraph 是默认 Runtime；SDK 是显式可选后端，不再形成第二套业务契约。

#### C. WebSocket 被业务执行阻塞（已解决）

WebSocket 事件现在先持久化到 Inbox，由 conversation scheduler 分发给 `MessageHandler`；Delivery 通过 Outbox 独立完成。

结果包括：

- 一个慢任务阻塞所有后续事件；
- 无法做到跨频道并发、同会话保序；
- 缺少排队、背压、超时、取消和任务恢复；
- 重启后无法判断任务是未开始、运行中、已完成还是已投递。

#### D. 请求状态仍包含全局可变部分（部分解决）

- 已解决：`ToolContext.current_post` 已替换为请求级不可变 `CapabilityContext`；
- `TraceContext` 使用全局可变字典；
- `working_memory` 由 `Agent` 直接维护；
- 全局 `config` 会在 SDK 初始化失败时被运行期修改。

文件能力的消息上下文已有并发隔离契约；全面引入并发后，仍可能出现 trace 串线、工作内存竞争和配置状态漂移等问题。

#### E. Memory 还不是企业上下文平台

[`memory.py`](../src/mmag/memory.py) 的 schema、migration 与 CJK FTS 预处理已经下沉到 [`infrastructure/sqlite`](../src/mmag/infrastructure/sqlite)，但它仍同时承担消息、URL 缓存、画像、知识和摘要等 Repository 职责。当前主要作用域仍是 `channel_id`，缺少：

- organization / tenant；
- team / department；
- project / customer；
- document / decision；
- task / task step / agent run；
- artifact / delivery；
- permission binding / audit event。

#### F. 安全与治理没有统一入口

治理入口已经完成第一轮收口：MCP 使用精确工具白名单并进入共享 Capability Policy，文件路径使用真实父子关系，stdio 子进程只继承最小环境，LangGraph/SDK 共享授权链，频道和用户参数可与请求资源动态匹配。仍缺组织/项目级 Scope Resolver、全部调用的持久审计和 Artifact 访问治理。

## 5. 重构目标与原则

### 5.1 目标

1. 入口可扩展：Mattermost 只是一个 Adapter；
2. 多人并发：跨会话并发，同一会话有序；
3. 多 Agent 可管理：可注册、发现、路由、授权、暂停和审计；
4. 上下文可继承：组织、项目、客户、会话和任务作用域明确；
5. 能力只定义一次：同一 Tool/Skill/MCP 能被不同 Runtime 使用；
6. 执行可恢复：任务、步骤和投递有持久状态；
7. 全链路可控：模型、数据、工具、产物和权限均可治理；
8. 渐进迁移：每一步都保持 Bot 可运行、可回滚。

### 5.2 设计原则

- **中枢是控制面**：负责理解、授权、路由、协调与汇总，不承担全部专业执行；
- **Agent 不等于 Tool**：Tool 是原子能力，Agent 是带策略、状态和结果契约的执行者；
- **上下文显式传递**：禁止依赖全局 `current_post` 等隐式请求状态；
- **策略先于执行**：每次模型、工具、数据访问先经过统一 Policy；
- **状态先于并发**：先定义任务状态、幂等和恢复，再扩大并发；
- **模块化单体优先**：先稳定边界，出现独立伸缩或隔离需求后再拆服务；
- **代码即事实**：目标能力未完成前必须在文档中标注为 Planned。

## 6. 目标架构

```mermaid
flowchart TB
    subgraph Entry[协作入口]
        MM[Mattermost Adapter]
        WEB[Web Workbench - Planned]
        API[API Adapter - Planned]
    end

    subgraph Hub[AI 协同中枢]
        INBOX[Event Inbox]
        COORD[Conversation Coordinator]
        SCOPE[Scope Resolver]
        INTENT[Intent Classifier]
        ROUTER[Task / Agent Router]
        RESULT[Result Assembler]
        OUTBOX[Delivery Outbox]
    end

    subgraph Managed[Managed Agents]
        AR[Agent Registry]
        LINK[Link Agent]
        RESEARCH[Research Agent]
        PROJECT[Project Assistant]
        CUSTOM[Custom Agents]
    end

    subgraph Runtime[统一执行层]
        RT[AgentRuntime]
        SDK[Claude SDK Adapter]
        LANG[LangGraph Adapter]
        CAP[Capability Catalog]
        MCP[MCP / CLI / Enterprise Systems]
    end

    subgraph Context[企业上下文]
        ORG[(Organization)]
        PROJ[(Project / Customer)]
        CONV[(Conversation / Message)]
        KNOW[(Document / Decision / Knowledge)]
        TASK[(Task / Run)]
        ART[(Artifact)]
    end

    subgraph Governance[治理底座]
        POLICY[Policy Engine]
        AUDIT[Audit Log]
        SECRET[Secret Provider]
        MODEL[Model Gateway]
        OBS[Metrics / Tracing]
    end

    Entry --> INBOX --> COORD
    COORD --> SCOPE --> INTENT --> ROUTER
    ROUTER <--> AR
    AR --> Managed
    Managed --> RT
    RT --> SDK
    RT --> LANG
    RT --> CAP --> MCP
    Context --> SCOPE
    Managed <--> Context
    Governance --> Hub
    Governance --> Runtime
    Managed --> RESULT --> OUTBOX --> Entry
```

### 6.1 协作入口层

职责：

- 将 Mattermost/Web/API 输入转换为统一 `InboundEvent`；
- 解析平台身份，但不做业务路由；
- 负责平台协议、重连、限流、附件下载/上传；
- 将结果从统一 `OutboundMessage` 转换为平台消息。

不负责：Prompt 构建、Agent 选择、任务执行、知识检索。

### 6.2 协同中枢

协同中枢是一组确定性应用服务，不是一个 Prompt：

- `ConversationCoordinator`：会话保序、并发和生命周期；
- `ScopeResolver`：解析组织、项目、客户、会话和用户作用域；
- `IntentClassifier`：识别聊天、查询、研究、生成产物等意图；
- `TaskPlanner`：把复杂请求转换为任务或 DAG；
- `AgentRouter`：按意图、权限、成本、可用性选择 Agent；
- `ResultAssembler`：汇总多个 Agent 输出、引用、附件和错误；
- `DeliveryService`：通过 Outbox 幂等投递。

### 6.3 Managed Agent

建议 Agent 声明以下元数据：

```yaml
name: research-agent
version: 1
accepted_intents:
  - research
  - compare
  - report
runtime: claude-sdk
model_policy: reasoning-medium
capabilities:
  - web.search
  - web.fetch
  - document.read
memory_scopes:
  - organization
  - project
  - conversation
permission_policy: research-readonly
timeout_seconds: 600
budget:
  max_model_calls: 12
  max_tool_calls: 30
result_schema: ResearchReport
```

Managed Agent 必须具备：

- 明确输入、输出和错误结构；
- 可配置能力白名单；
- 上下文读取和写入范围；
- 模型、超时、重试和成本预算；
- 执行记录和审计信息；
- 取消、降级和健康状态。

### 6.4 Capability Catalog

Capability 是 Tool、Skill、MCP 或企业 API 的统一抽象。一个能力只能有一个事实定义：

```python
@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    risk_level: str
    required_permissions: tuple[str, ...]
    timeout_seconds: int
    handler: CapabilityHandler
```

不同 Runtime 只负责把它转换成对应协议：

```text
CapabilitySpec
  ├── Anthropic Tool Schema Adapter
  ├── Claude SDK @tool Adapter
  ├── MCP Adapter
  └── Internal API Adapter
```

## 7. 核心领域模型

### 7.1 请求级契约

```python
@dataclass(frozen=True)
class RunContext:
    trace_id: str
    tenant_id: str
    actor_id: str
    conversation_id: str
    project_id: str | None
    source_event_id: str
    permissions: frozenset[str]
    locale: str
```

`RunContext` 必须由入口和 Scope Resolver 构建，显式传给中枢、Agent、Capability 和审计服务。禁止继续使用全局单槽保存当前消息。

### 7.2 统一生命周期与状态机

```text
PENDING → QUEUED → RUNNING → SUCCEEDED
                   │    ├── FAILED
                   │    ├── TIMED_OUT
                   │    └── CANCELLED
                   └── WAITING_APPROVAL
```

该图表达 `Task/AgentRun` 的主生命周期，不代表所有实体共用一组状态。系统应建立统一的状态转换协议和 `LifecycleService`，再为下列实体定义独立状态机：

- `Task/AgentRun`：排队、执行、待审批与终态；
- `CapabilityCall`：计划、执行、待审批、拒绝与结果；
- `ApprovalRequest`：待处理、已批准、已驳回、已过期与已取消；
- `Delivery`：待投递、投递中、重试等待、已投递与终态失败。

每次 Agent 执行对应一个 `AgentRun`，每个工具调用对应一个 `CapabilityCall`。回复是否成功投递由独立 `Delivery` 状态记录。

所有转换必须通过同一生命周期端口执行：校验合法前置状态，使用幂等键与乐观版本防止重复副作用，原子更新当前状态并追加不可变转换历史。转换记录至少包含实体 ID、前后状态、版本、原因、actor/event、trace 和时间。

Runtime 只返回后端中立结果，不拥有业务状态。SDK 与 LangGraph 的结果必须映射到同一 `AgentRunState`；挂起、审批、投递和恢复由 Runtime 之上的应用控制面管理。

### 7.3 上下文作用域

上下文读取顺序建议为：

```text
当前消息
  → 当前 thread / conversation
  → 当前 task / project
  → 当前 customer / department
  → organization shared knowledge
```

作用域越大，检索优先级越低，权限要求越高。任何上下文片段都应保留来源、更新时间、可见范围和置信度。

### 7.4 关键实体关系

```mermaid
erDiagram
    ORGANIZATION ||--o{ ACTOR : contains
    ORGANIZATION ||--o{ PROJECT : owns
    ORGANIZATION ||--o{ AGENT_SPEC : manages
    PROJECT ||--o{ CONVERSATION : scopes
    PROJECT ||--o{ DOCUMENT : contains
    PROJECT ||--o{ DECISION : records
    CONVERSATION ||--o{ MESSAGE : contains
    CONVERSATION ||--o{ TASK : creates
    TASK ||--o{ TASK_STEP : decomposes
    TASK_STEP ||--o{ AGENT_RUN : executes
    AGENT_SPEC ||--o{ AGENT_RUN : performs
    AGENT_RUN ||--o{ CAPABILITY_CALL : invokes
    AGENT_RUN ||--o{ ARTIFACT : produces
    ACTOR ||--o{ AGENT_ASSIGNMENT : owns
    AGENT_SPEC ||--o{ AGENT_ASSIGNMENT : assigned
```

## 8. 关键运行流程

### 8.1 普通群聊消息

```text
1. Mattermost Adapter 接收 posted event
2. 标准化为 InboundEvent，按 event_id 幂等入 Inbox
3. Conversation Worker 按 conversation_id 取出事件
4. Scope Resolver 解析用户、组织、项目和权限
5. Context Assembler 构建最小必要上下文
6. Intent Classifier 判断无需响应 / 中枢直接回答 / 创建任务
7. Agent Router 选择 AgentDescriptor / Agent Package
8. Runtime 执行，Capability 调用逐次经过 Policy
9. Result Assembler 汇总文本、来源和产物
10. Outbox 幂等投递到 Mattermost
11. 持久化 Run、调用记录、成本和审计事件
```

### 8.2 复杂任务与多 Agent 协作

复杂请求不应由一个 Agent 无限调用工具。Task Planner 产生受限任务图：

```text
用户请求“整理竞品并生成汇报”
  ├── Research Agent：采集和核验资料
  ├── Analysis Agent：结构化比较
  └── Presentation Agent：生成交付物
                         ↓
                   Result Assembler
```

每个步骤使用结构化输入输出；中枢只传递必要上下文和 Artifact 引用，避免复制全部对话。

### 8.3 专属数字员工

个人会话中的专属 Agent 通过 `AgentAssignment` 决定：

- 绑定用户、项目或会话；
- 继承哪些组织上下文；
- 能使用哪些 Capability；
- 是否允许产生外部副作用；
- 哪些行为需要人工审批。

## 9. 权限、安全与治理

### 9.1 统一策略模型

每次执行都应形成如下策略输入：

```text
Subject：当前用户 / Agent
Action：read / search / call / write / deliver
Resource：消息 / 项目 / 文件 / MCP 工具 / 企业系统
Context：组织、会话、任务、时间、风险级别
```

策略结果：

- `ALLOW`：允许；
- `DENY`：拒绝并记录原因；
- `REQUIRE_APPROVAL`：等待人工确认；
- `ALLOW_WITH_REDACTION`：脱敏后允许。

### 9.2 必需治理能力

- MCP Server 和 Capability 显式白名单；
- 子进程最小环境变量注入；
- 文件路径使用真实父子关系判断，不使用字符串前缀；
- URL 访问统一复用 SSRF、防重定向和响应大小限制；
- Secret Provider，不把凭据放入 Prompt、日志或通用 RunContext；
- 模型网关统一处理模型路由、限流、重试、成本和内容策略；
- 审计记录用户、Agent、模型、工具、数据作用域、输入摘要、输出摘要和结果；
- 写操作、外发、删除、审批等高风险能力默认需要更强策略。

## 10. 建议代码结构

迁移期采用模块化单体：

```text
src/mmag/
├── bootstrap.py
├── domain/
│   ├── conversation.py
│   ├── context.py
│   ├── task.py
│   ├── agent.py
│   └── capability.py
├── application/
│   ├── collaboration_hub.py
│   ├── conversation_coordinator.py
│   ├── context_assembler.py
│   ├── task_planner.py
│   ├── agent_router.py
│   ├── result_assembler.py
│   └── delivery_service.py
├── agents/
│   ├── registry.py
│   ├── link_agent.py
│   ├── research_agent.py
│   └── project_assistant.py
├── runtimes/
│   ├── base.py
│   ├── claude_sdk.py
│   └── langgraph.py
├── capabilities/
│   ├── catalog.py
│   ├── executor.py
│   ├── policy.py
│   └── adapters/
├── ports/
│   ├── inbound.py
│   ├── outbound.py
│   ├── repositories.py
│   ├── model_gateway.py
│   └── audit.py
├── infrastructure/
│   ├── mattermost/
│   ├── sqlite/
│   ├── mcp/
│   ├── llm/
│   └── observability/
└── config/
```

依赖方向应保持：

```text
infrastructure/adapters → application → domain
```

领域层不得导入 Mattermost、Anthropic SDK、Claude Agent SDK、MCP 或 SQLite。

## 11. 分阶段迁移计划

### Phase 0：建立安全基线

目标：重构前先证明关键行为。

工作项：

- [x] 建立隔离的 test suite，PoC 和真实服务测试不进入默认集合；
- [x] 增加首条离线 `Message → Route → Runtime → Reply` 主链契约测试；
- [x] 补齐附件、工具、多轮、重试和幂等场景；
- [x] 引入数据库 schema version 和正式 migration；
- [x] 建立 CI：ruff、pytest、coverage、类型检查；
- [x] 修复 wheel 未包含运行资源的问题；
- [x] 校正文档与当前代码的版本漂移。

退出标准：

- 默认测试不访问真实 Mattermost/LLM/公网；
- 关键链路测试稳定通过；
- 旧数据库升级有自动化测试和回滚方案；
- CI 可作为后续重构门禁。

### Phase 1：统一 Runtime 与 Capability

目标：消除 SDK/Legacy 双轨业务差异。

工作项：

- [x] 定义 `AgentRuntime`、`RunRequest`、`AgentResult`；
- [x] 定义单一 `CapabilitySpec` 和 `CapabilityExecutor`；
- [x] 从同一 Capability 生成 SDK 与 CapabilityRegistry binding，并将 MCP discovery 适配为 Capability；
- [x] 统一能力来源、执行结果和 Policy 入口；
- [x] Runtime 统一错误、deadline 和 fallback 语义；
- [x] MemoryCompactor 通过 Runtime Port 调用模型；
- [x] 选定默认 Runtime，另一套仅作为启动回退；
- [x] 删除共享内置工具的重复实现。
- [x] 八个内置能力由同一有序 Catalog 生成，两个 Runtime 可见性一致。
- [x] 将 `send_file` 纳入写能力策略，并消除全局当前消息槽。
- [x] 删除 SDK crawl 旁路，外部 MCP 在两个 Runtime 中共享白名单和授权结果。

退出标准：

- [x] 共享内置能力只存在一份 schema、handler 和策略；
- [x] 两个 Runtime 通过同一组契约测试；
- [x] 上层不再判断 SDK/Legacy 特有异常；
- [x] MCP 能力对不同 Runtime 的可见性一致。

### Phase 2：解耦入口和执行

目标：支持安全并发和任务恢复。

工作项：

- [x] 引入 `InboundEvent`、Inbox 和 Outbox；
- [x] 使用 `conversation_id` 分区：同会话串行、跨会话并发；
- [x] 将附件、摘要和长任务移出 WebSocket 读取循环；
- [x] 扩展不可变 `RunContext` 和 `ContextVar` 日志上下文；
- [x] 删除全局 `current_post`，先建立请求级 `CapabilityContext`；
- [x] 增加幂等、背压、超时、取消和优雅关闭；
- [x] Mattermost 事件路径迁移为有 timeout/retry 的异步 Client。

退出标准：

- 慢 LLM 不阻塞其他频道事件；
- 同一会话内消息顺序稳定；
- 重启后可恢复未完成或未投递任务；
- 并发测试不存在上下文和 trace 串线。

### Phase 3：企业上下文与持久状态

目标：从频道记忆升级为有作用域的上下文平台。

工作项：

- [x] 建立统一 `LifecycleService` 与实体级状态转换矩阵；
- [x] 持久化当前状态、乐观版本和 append-only 转换历史；
- [x] 建立 `WAITING_APPROVAL` 挂起/恢复语义与 `ApprovalRequest` 最小模型；
- [x] 建立重启恢复与 reconciliation；
- [x] 拆分 Message、Profile、Knowledge、Summary、URL Cache Repository；
- [x] 新增企业实体、任务、运行、能力调用、审批、产物、投递和审计模型；
- [x] 建立 Scope Resolver 与 Context Assembler；
- [x] 上下文记录来源、时间和置信度；
- [x] 为 SQLite 建立 WAL、事务和单写者策略。

退出标准：

- 所有生命周期转换经过同一端口，非法转换和重复命令被确定性拒绝；
- 重启后可恢复未完成运行、待审批请求和待投递结果；
- SDK 与 LangGraph 对外呈现相同的任务状态语义；
- 可按组织/项目/会话安全检索上下文；
- 每次 Agent 执行和产物均可追踪；
- Context Assembler 不依赖 Mattermost 具体结构；
- Memory 大类不再拥有所有表和业务逻辑。

### Phase 4：Managed Agent 与路由

目标：让数字员工成为平台的一等公民。

工作项：

- [x] 实现 `AgentDescriptor`、`ManagedAgent`、`AgentRegistry`；
- [x] 实现基于意图、权限、作用域、成本和健康度的 Router；
- [x] 将 `analyze_link` 升级为第一个 Link Agent；
- [x] 实现 Agent handoff 和结构化 Artifact；
- [x] 删除无 Package/Schema/Policy 的 Research、Project、Presentation 占位 Agent；
- [x] `mmchat` 与 `link` 通过扁平 Package、可信 Provider 和 AgentFactory 自动注册；
- 引入 AgentAssignment 支持个人/项目专属数字员工。

退出标准：

- 新 Agent 可通过注册配置接入，而不是修改核心 `Agent`；
- Agent 的能力、权限、预算和输出均可检查；
- 多 Agent 任务有明确步骤和状态；
- 单个 Agent 失败不会破坏整个会话处理。

### Phase 5：治理与私有化生产能力

目标：达到企业可控、可审计、可运维。

工作项：

- [x] Policy Engine、审批节点和敏感数据脱敏；
- [x] Secret Provider 与最小权限执行边界；
- [x] Model Gateway、模型路由、配额和成本统计；
- [x] metrics、任务级 tracing 和审计关联；
- [x] 数据清理和 SQLite 在线备份恢复原语；
- [x] 私有化部署拓扑、升级和灾备文档；
- [x] 离线并发/安全契约测试；目标环境压测作为部署验收执行。

## 12. 测试策略

### 12.1 测试分层

| 层级 | 覆盖内容 | 是否允许真实外部服务 |
|---|---|---|
| Unit | Domain、Router、Policy、Context 选择 | 否 |
| Contract | Runtime、Capability、Repository、Adapter | 否，使用 fake server |
| Integration | SQLite/MCP/Mattermost 协议组合 | 仅本地容器或 fake |
| E2E | 完整消息到回复链路 | 独立环境，显式执行 |
| PoC/Benchmark | 真实模型、成本、延迟、SDK 行为 | 显式执行，不进入默认 CI |

### 12.2 必须锁定的关键场景

- 重复 WebSocket event 不会重复回复；
- 同会话保序、跨会话并发；
- SDK 和 LangGraph 对相同 Capability 的行为一致；
- Capability 权限拒绝不会被 Runtime 绕过；
- LLM 限流、超时和内容拒绝有不同错误语义；
- Agent 成功但投递失败时可重试，不重复执行 Agent；
- SQLite migration 中断后不会产生半迁移状态；
- Agent handoff 不泄漏其他项目或用户上下文；
- 审计记录不包含原始 Secret。

## 13. 可观测性与验收指标

建议建立以下基础指标：

- `inbound_events_total` / `inbound_lag_seconds`；
- `conversation_queue_depth`；
- `agent_runs_total{agent,status}`；
- `agent_run_duration_seconds`；
- `model_calls_total{model,status}`；
- `model_tokens_total` / `model_cost_total`；
- `capability_calls_total{capability,status}`；
- `policy_decisions_total{decision}`；
- `deliveries_total{channel,status}`；
- `context_retrieval_duration_seconds`；
- `dropped_events_total` / `duplicate_events_total`。

架构迁移不能只用“文件变小”作为成功标准，应关注：

- 新增 Agent 是否无需修改中枢；
- 新增 Runtime 是否无需复制工具；
- 单个任务是否可追踪、取消和恢复；
- 权限是否可解释；
- 多频道并发是否不串上下文；
- 失败是否能被用户和运维感知。

## 14. 主要风险与控制措施

| 风险 | 影响 | 控制措施 |
|---|---|---|
| 在测试基线前移动大量代码 | 回归无法定位 | Phase 0 先建契约测试 |
| 直接拆微服务 | 运维复杂度超过收益 | 模块化单体优先 |
| 新旧 Runtime 长期共存 | 行为继续漂移 | 统一协议并明确淘汰窗口 |
| 先上多 Agent | 放大重复、并发和权限问题 | Runtime/Capability/Queue 先行 |
| 数据模型一次性过度设计 | 迁移成本过高 | 从 Task/Run/Artifact/Scope 最小集合开始 |
| Prompt 承担权限和路由 | 不可验证、可被绕过 | 确定性 Policy 和 Router |
| SQLite 并发策略不清 | 锁、损坏或数据不一致 | 单写者/事务边界/Repository 测试 |
| 文档愿景被误认为现状 | 错误决策 | 明确 Current/Planned 状态并持续更新 |

## 15. 下一步实施思路

### 15.1 总体方法

后续重构遵循六条原则：

1. **契约先于搬文件**：先用输入、输出、错误和测试定义边界，再移动实现。
2. **适配先于替换**：现有 SDK 和 LangGraph 先包进统一 Adapter，不在同一迭代重写其内部逻辑。
3. **垂直切片先于批量迁移**：先迁移一个只读 Capability，跑通 schema、执行、来源和审计，再迁移其余工具。
4. **外部行为保持稳定**：Phase 1 不改变 Mattermost 触发方式、回复格式和数据库格式。
5. **数据演进必须可验证**：所有持久化变化继续使用 forward-only migration，并覆盖旧库升级和失败回滚。
6. **保持模块化单体**：当前优先建立清晰模块边界，不提前引入微服务、分布式队列或复杂控制面。

### 15.2 实施包 A：收口 Phase 0（完成）

目标：把本地已通过的基线变成每次变更都必须通过的工程门禁。

实施内容：

- [x] 增加 CI，执行 Ruff、默认离线 pytest、coverage 和宽松模式类型检查；
- [x] 首次采集 branch coverage，设置 40% 的可持续初始阈值；
- [x] 构建 wheel 后在隔离目录执行包、CLI 模块和 Agent Package Prompt 加载 smoke test；
- [x] 将 external/PoC 测试保留为显式任务，不让密钥和公网依赖进入默认 CI；
- [x] 覆盖附件、工具和多轮调用；
- [x] 补齐失败重试和重复事件测试。

验收标准：

- [x] 干净环境中一条命令可完成 lint、测试、类型检查和构建验证；
- [x] 默认 CI 不访问真实 Mattermost、LLM 或公网；
- [x] wheel 解包后无需仓库源码目录即可导入并加载运行资源；
- [x] 门禁失败能明确指出是代码质量、行为、类型还是打包问题；
- [x] 失败重试和重复事件行为有稳定契约。

### 15.3 实施包 B：建立 Runtime 契约（完成）

目标：让应用层只依赖统一执行协议，不感知 Claude Agent SDK 或 LangGraph 细节。

实施内容：

- [x] 定义不可变 `RunContext`、`RunRequest` 和结构化 `AgentResult`；
- [x] 定义 `AgentRuntime` Protocol，以及 timeout、rate-limit、rejected、unavailable 等统一错误；
- [x] 用现有实现包装 SDK/LangGraph Adapter，保持外部行为不变；
- [x] 将 `Agent`、`MemoryCompactor` 改为依赖 Runtime Port；
- [x] 形成默认 Runtime 和 Legacy 淘汰策略 ADR。

验收标准：

- [x] `Agent` 不再直接判断 Runtime 私有异常或返回结构；
- [x] 两个 Adapter 通过同一组 contract tests；
- [x] 切换 Runtime 不改变消息路由和回复投递接口。

### 15.4 实施包 C：Capability 单一来源（完成）

目标：让 `capabilities/` 成为 schema、handler、授权和格式化逻辑的单一来源。

实施内容：

- [x] 定义 `CapabilitySpec`、`CapabilityResult`、权限/副作用元数据和执行器；
- [x] 选择 `get_channel_info` 完成首个只读切片；
- [x] 从同一 Capability 生成 CapabilityRegistry/SDK binding，并将 MCP discovery 纳入同一 Catalog/Policy；
- [x] 统一共享内置能力的来源信息、超时和错误字段；
- [x] 逐个迁移共享内置工具，删除重复实现。
- [x] 建立单一有序内置 Catalog，SDK/Legacy 不再维护各自的装配清单；
- [x] 将文件发送迁移为受授权的写 Capability，并建立请求上下文隔离；
- [x] 删除 SDK crawl 重复工厂和 formatter，权限回调只放行实际可见能力；

验收标准：

- [x] 首个切片只有一份输入 schema 和 handler；
- [x] SDK/LangGraph 对首个切片的相同输入产生等价结果；
- [x] 写能力能被明确标记，并为后续审批策略保留边界。
- [x] 外部 MCP 在 SDK/LangGraph 中具有相同 schema、来源和 Policy 结果。

### 15.5 实施包 D：入口与执行解耦

目标：解决 WebSocket 读取循环等待完整 LLM/工具链的问题，为多人并发和任务恢复打基础。

实施内容：

- 定义与 Mattermost 无关的 `InboundEvent`；
- 以 `conversation_id` 分区，同会话串行、跨会话并发；
- 将执行结果写入 Outbox，再由投递器发送；
- 建立事件幂等、背压、取消、超时和优雅关闭语义；
- [x] 删除全局 `current_post`，使用不可变上下文贯穿当前能力调用；

验收标准：

- 慢任务不阻塞其他频道；
- 重复事件不重复执行或回复；
- Agent 成功但投递失败时只重试投递；
- 并发测试中不存在会话、文件和 trace 串线。

### 15.6 后续阶段

入口与执行稳定后，再依次推进：

1. 建立统一生命周期端口、状态转换矩阵、持久历史和重启恢复；
2. 拆分 Message/Profile/Knowledge/Summary Repository，建立最小 Scope、Task、Run、CapabilityCall、ApprovalRequest、Artifact 和 Delivery 模型；
3. 在统一状态机之上实现审批通知、审批人鉴权、批准/驳回/过期与幂等恢复；
4. 引入 Agent Registry 和确定性 Router，将 Link Agent 作为首个 Managed Agent；
5. 最后增加模型网关、审计、保留策略和私有化运维能力。

在 Runtime、Capability 和执行队列稳定前，不新增第二个 Managed Agent，也不进行微服务拆分。

## 16. 待确认的架构决策

以下问题需要在实施前形成 ADR：

1. Claude Agent SDK 是否作为唯一长期 Runtime，LangGraph 是回退还是最终删除；
2. 任务状态先落 SQLite，还是直接使用可独立部署的队列/数据库；
3. organization/project/customer 与 Mattermost team/channel 的映射规则；
4. Agent Router 首期采用确定性规则、LLM 分类还是混合方案；
5. 哪些 Capability 被定义为有副作用，哪些必须人工审批；
6. Artifact 使用 Mattermost 文件、对象存储还是二者结合；
7. 私有化部署需要支持的模型提供商、数据库和 Secret 系统；
8. 企业上下文的保留、删除、脱敏和跨项目共享规则。

## 17. 与现有文档的关系

- [`README.md`](../README.md)：当前产品能力和运行方式；
- [`ROADMAP.md`](./ROADMAP.md)：现有版本里程碑；
- [`TECH_DEBT.md`](./TECH_DEBT.md)：已有问题清单；
- 本文档：AI Native 目标架构、演进原则和迁移顺序。

后续一旦正式采用本方案，应同步修订 Roadmap：将“0.2.0 架构清理”从按大文件拆分，调整为“契约基线 → Runtime/Capability 统一 → 执行解耦 → Context → Managed Agent”的渐进路线。

## 18. 结论

mmag 当前的优势是已有真实协作入口、工具、记忆和多模态能力，不需要推倒重来。真正需要改变的是系统控制权：

```text
从：一个 Bot 对消息做完整处理
到：一个协同中枢协调上下文、任务、Agent、能力和治理
```

只有先完成 Runtime、Capability、RunContext 和任务状态的统一，链接、研报、PPT、项目助理等数字员工才能成为可复用、可并发、可授权、可审计的企业能力，而不是继续堆积在单个 `Agent` 中的功能分支。
