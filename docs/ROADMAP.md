# Roadmap

> 状态：Active
>
> 更新时间：2026-07-31
>
> 当前阶段：Step 4–7 平台控制面基线完成，LangGraph 原生 HITL 已接通，进入生产验证
>
> 架构依据：[`AI_NATIVE_REFACTORING.md`](./AI_NATIVE_REFACTORING.md)

本文档是 mmag 的可执行路线图，回答“下一步做什么、先后依赖是什么、做到什么算完成”。目标架构和设计理由由 `AI_NATIVE_REFACTORING.md` 维护，具体问题由 [`TECH_DEBT.md`](./TECH_DEBT.md) 跟踪。

## 1. 当前基线

截至 2026-07-31，已经完成：

- [x] PoC 和真实外部服务测试退出默认 pytest 集合；
- [x] 建立完全离线的消息主链契约测试；
- [x] 修复 Runtime 失败提示被发送层静默丢弃的问题；
- [x] 建立 SQLite schema version 和 forward-only migration；
- [x] 覆盖新库初始化、旧库升级、FTS 重建、幂等、回滚和未来版本拒绝；
- [x] 将 schema、migration 和 CJK FTS 预处理从 `Memory` 下沉到 infrastructure；
- [x] 修复消息/FTS 半事务、Secret 日志和路径前缀越界问题；
- [x] SDK 与外部 MCP 工具改为显式白名单，未知能力默认拒绝；
- [x] 建立 GitHub Actions 与 `make verify` 统一工程门禁；
- [x] 建立 40% 分支覆盖率与 `src/mmag` mypy 基线；
- [x] 提交 `uv.lock`，固定 CI 与本地开发依赖；
- [x] 将默认 Prompt 打入 wheel，并在隔离目录验证包、CLI 模块和 Prompt 加载；
- [x] 重复 posted 事件在 Runtime 前按持久化 post ID 去重；
- [x] Mattermost 回复使用 `pending_post_id` 对网络错误、超时、429/5xx 做有界幂等重试；
- [x] 默认离线测试基线达到 `255 passed, 2 deselected`，实际分支覆盖率 `59.15%`；
- [x] 建立不可变 Runtime 输入/输出与统一错误模型；
- [x] LangGraph 成为默认 Runtime，接入 SQLite checkpoint、原生 interrupt/resume；
- [x] 审批支持 approve/edit/reject，并覆盖跨 Runtime 重建恢复；
- [x] `Agent` 与 `MemoryCompactor` 已只依赖 `AgentRuntime` Port；
- [x] 建立 Capability 核心契约，八个内置能力均已迁移到单一 Spec；
- [x] 删除全局 `ToolContext.current_post`，文件能力改用请求级不可变上下文。
- [x] 外部 MCP discovery 已适配为 Capability，共享 Spec、Policy 和可见性。

下一阶段尚未完成：

- [x] WebSocket 入口、长任务执行和结果投递已解耦；
- [x] 企业 Context、持久生命周期、Managed Agent 和治理基线已建立。

## 2. 实施原则

1. **依赖顺序优先**：工程门禁 → Runtime → Capability → 执行解耦 → Context → Managed Agent → 治理。
2. **契约优先**：每个边界先定义类型、错误和测试，再迁移调用方。
3. **垂直切片**：先迁移一个能力验证全链路，不批量复制新结构。
4. **兼容迁移**：Mattermost 交互和现有数据库在过渡期保持兼容。
5. **单一事实来源**：工具 schema、handler、权限和结果格式不能继续在两个 Runtime 中分别维护。
6. **模块化单体**：没有容量或隔离证据前，不拆微服务，不引入分布式基础设施。

## 3. 推荐实施顺序

```text
Phase 0 工程门禁
  ↓
Runtime 契约与 Adapter
  ↓
Capability 单一来源
  ↓
入口 / 执行 / 投递解耦
  ↓
企业 Context 与任务状态
  ↓
Managed Agent 与 Router
  ↓
治理、审计与私有化运维
```

前一阶段的退出标准，是后一阶段开始大规模改动的前置条件。

## 4. Step 1：收口 Phase 0 工程门禁（已完成）

### 目标

把当前本地基线固化为自动门禁，并证明发布产物可以脱离源码仓库运行。

### 工作项

- [x] 引入 `pytest-cov`，当前分支覆盖率 `47.73%`，初始阈值 40%；
- [x] 引入宽松模式 `mypy`，检查 `src/mmag`，当前 33 个源码文件零错误；
- [x] 建立 `.github/workflows/ci.yml`，使用锁定依赖执行统一门禁；
- [x] 增加 wheel smoke test：隔离解包后验证 `import mmag`、CLI 模块和 Prompt 加载；
- [x] 将 `prompts.yml` 打为包资源，并支持 `PROMPTS_PATH` 显式覆盖；
- [x] 覆盖附件、工具和多轮调用契约；
- [x] 补齐失败重试和重复事件契约；
- [x] 提供 `make verify` 统一门禁命令，使本地与 CI 使用相同入口。

### 实施思路

- CI 默认不注入 Mattermost/LLM 密钥，也不访问公网；
- external 和 PoC 测试保留为手动任务，不作为普通变更的稳定性信号；
- coverage 首先用于暴露盲区，不为了数字给简单代码堆测试；
- 类型检查先锁定新增代码和核心契约，历史问题分批收敛；
- wheel smoke test 在临时隔离环境执行，确保不是依赖 editable install 偶然通过。

### 退出标准

- [x] 干净环境中一条命令可以执行完整工程门禁；
- [x] CI 默认流程完全离线且稳定；
- [x] wheel 解包后可以正常导入包与 CLI 模块并加载 Prompt；
- [x] coverage 和类型检查有明确、不会倒退的基线；
- [x] SQLite migration 和现有消息主链通过全部回归测试；
- [x] 失败重试和重复事件行为由契约测试固定。

## 5. Step 2：建立统一 Runtime 契约（已完成）

### 目标

让应用层只表达“执行一次 Agent Run”，不再依赖 SDK/LangGraph 的参数、返回结构和异常。

### 工作项

- [x] 定义不可变 `RunContext`：trace、actor、conversation、scope 和 deadline；
- [x] 定义 `RunRequest`：消息（含多模态 content blocks）、可用能力和执行配置；
- [x] 定义 `AgentResult`：文本、结构化产物、能力调用、usage 和状态；
- [x] 定义 `AgentRuntime` Protocol；
- [x] 统一 timeout、rate-limit、rejected、unavailable 和 internal 错误语义；
- [x] 为 SDKLLM 和 LangGraph 建立统一 Runtime 边界；
- [x] 让 `Agent`、`MemoryCompactor` 等调用方依赖 Runtime Port；
- [x] 修订 Runtime ADR，确立 LangGraph 默认路径并移除 Legacy Adapter。

### 实施思路

第一轮只包裹现有实现，不同时重写 Agent 循环。先通过 contract tests 证明两个 Adapter 对统一请求和错误语义的行为一致，再逐步迁移调用方。

### 退出标准

- [x] 上层代码不导入两套 Runtime 的私有类型和异常；
- [x] 两个 Adapter 通过同一组契约测试；
- [x] 切换 Runtime 不改变 Mattermost 路由和投递协议；
- [x] 每次运行都有稳定的 trace、状态和错误分类。

## 6. Step 3：统一 Capability（已完成）

### 目标

消除 `tools/builtin.py` 与 `sdk_tools.py` 的重复定义，让能力成为可授权、可测试、可审计的一等对象。

### 工作项

- [x] 定义 `CapabilitySpec`、`CapabilityResult` 和 `CapabilityExecutor`；
- [x] 在 Spec 中声明输入 schema、只读/写入属性、权限、超时和来源策略；
- [x] 以 `get_channel_info` 完成第一个只读垂直切片；
- [x] 从同一 Spec 生成 ToolRegistry 与 SDK binding；
- [x] 统一两条 Runtime 的返回结构、错误和来源信息；
- [x] 逐个迁移共享内置工具，删除重复 handler/formatter；
- [x] 收紧 MCP 默认权限和文件路径判断。
- [x] 外部 MCP 先进入 Capability Catalog/Policy，再生成 LangGraph 与 SDK binding。

### 实施思路

不先设计覆盖所有未来 Agent 的万能抽象。首个切片只需要证明“一份 schema + 一份 handler + 多 Runtime binding”成立，再根据第二、第三个能力暴露出的差异扩展协议。

当前八个内置能力由同一个有序 Catalog 创建，再派生 LangGraph/SDK binding；每项能力只有一个 Spec、JSON Schema 和 handler。统一执行器负责参数校验、deadline、来源和稳定错误码；`CapabilityAuthorizer` 在副作用前返回允许、拒绝或待审批，LangGraph 将待审批转换为原生 interrupt。

`send_file` 也已成为声明 `mattermost:file:write` 的 Capability。普通异步链路用 `ContextVar` 绑定不可变 `CapabilityContext`；Claude SDK 的持久 MCP reader 无法继承后续 task context，因此 SDK 查询通过实例级锁串行执行，并在查询生命周期内桥接、清理同一份上下文。该设计消除了跨频道覆盖 `current_post` 的竞态，但不等于 Step 4 的跨会话并行调度已经完成。

外部 MCP 工具只发现一次：白名单命中的工具被转换为 `CapabilitySpec`，缺少 `readOnlyHint=true` 时保守视为写能力，并声明独立执行权限；随后同一 Spec 与 `CapabilityExecutor` 生成 ToolRegistry/SDK binding。SDK 权限回调由实际绑定集合动态生成，不再硬编码 crawl 能力。原 `sdk_crawl_tools.py` 及其重复 schema/formatter 已删除，crawl 等工具必须通过 `.mcp.json` 和 `MCP_ALLOWED_TOOLS` 显式接入。

### 下一步顺序

1. [x] 迁移 `search_knowledge`，验证带可选参数、默认值的本地读取能力；
2. [x] 迁移 `get_posts`，统一缓存命中、REST 回退、回填与参数上限，并将同步 I/O 移入工作线程；
3. [x] 迁移 `search_messages`，统一多条件过滤、时间单位、参数上限与结果格式；
4. [x] 迁移 `get_user_profile`，收口 Memory 与 Mattermost 的组合读取；
5. [x] 迁移 `analyze_link`，让 `SourcePolicy.AUTO` 真正驱动来源注入；
6. [x] 迁移 `save_knowledge`，为 `WRITE` 能力接入确定性的权限检查和未来审批钩子；
7. [x] 将 `send_file` 迁移为受治理的写 Capability，并用请求级不可变上下文消除全局 `ToolContext.current_post`；
8. [x] 让外部 MCP 进入同一 Catalog/Policy 可见性链路，并删除两套重复工厂与 formatter。

### 退出标准

- [x] 共享内置能力只有一份 schema、handler 和策略；
- [x] SDK/LangGraph 对共享内置能力的成功和失败结果等价；
- [x] 写能力能被确定性识别，后续可挂接审批；
- [x] MCP 能力可见性不能绕过 Capability Policy。

## 7. Step 4：解耦入口、执行和投递

### 目标

支持同会话保序、跨会话并发，以及执行成功后投递失败的独立恢复。

### 工作项

- [x] 定义平台无关的 `InboundEvent`；
- [x] 建立 Inbox 幂等记录；
- [x] 按 `conversation_id` 分区调度：同会话串行、跨会话并发；
- [x] 将附件处理、摘要和长任务移出 WebSocket 读取循环；
- [x] 建立 Outbox 与独立 Delivery；
- [x] 增加背压、取消、deadline 和优雅关闭；
- [x] 删除全局 `current_post`，先用不可变 `CapabilityContext` 贯穿能力调用；
- [x] Mattermost 事件路径迁移到带 timeout/retry 和连接池的异步 Client。

### 退出标准

- [x] 慢 LLM 不阻塞其他频道；
- [x] 同一会话顺序稳定，重复事件不重复回复；
- [x] 投递失败只重试 Delivery，不重复执行 Agent；
- [x] 并发测试不存在上下文、附件和 trace 串线；
- [x] 关闭进程时不丢失已接受但尚未投递的任务。

## 8. Step 5：企业 Context 与持久任务状态

### 目标

将“频道消息记忆”升级为有作用域、有来源、可追踪的企业上下文，并建立一套两个 Runtime 共用的持久生命周期。

### Step 5.1：统一生命周期与状态机

统一的是状态转换协议、持久化、幂等和审计规则，不是把所有对象塞进一个巨型状态机。`Task/AgentRun`、`CapabilityCall`、`ApprovalRequest` 和 `Delivery` 各自拥有状态集和转换矩阵，但只能通过同一 `LifecycleService` 发生转换。

工作项：

- [x] 定义五类实体状态、初始态、终态和合法转换；
- [x] 定义通用 `StateTransition`；
- [x] 建立 `LifecycleService`，集中处理转换守卫、幂等、乐观版本和终态保护；
- [x] 通过 migration 持久化当前状态、版本和 append-only 转换历史；
- [x] 定义 Runtime 结果到 `AgentRunState` 的统一映射；
- [x] 为 `WAITING_APPROVAL` 保存 Capability 参数快照和恢复令牌；
- [x] 建立重启恢复与 reconciliation；
- [x] 增加状态转换、并发竞争、重复事件、非法转换和重启恢复契约测试。

退出标准：

- [x] 生命周期状态通过 `LifecycleService` 改变；
- [x] 非法转换被确定性拒绝，重复命令不会重复执行副作用；
- [x] 进程重启后可识别并恢复未完成运行、待审批请求和待投递结果；
- [x] SDK 与 LangGraph 对外呈现相同的任务生命周期；
- [x] Agent 执行状态与 Delivery 状态彼此独立。

### Step 5.2：企业 Context 与持久模型

#### 工作项

- [x] 拆分 Message、Profile、Knowledge、Summary 和 URL Cache Repository；
- [x] 建立最小持久任务、审批、产物、投递和审计模型；
- [x] 增加 Organization、Project、Customer、Document 和 Decision 的通用映射；
- [x] 实现 Scope Resolver 与 Context Assembler；
- [x] 明确 SQLite 单写者、WAL、事务和备份策略；
- [x] 所有数据模型变化通过 v004 migration 发布。

#### 退出标准

- [x] 上下文可按组织、项目、会话安全检索；
- [x] Run、Capability Call、Artifact 和 Delivery 可以关联追踪；
- [x] 任务和运行状态可持久恢复，转换有版本和审计记录；
- [x] Context Assembler 不依赖 Mattermost 原始事件结构；
- [x] `Memory` 已成为兼容 Facade，读取职责由专用 Repository 承担。

## 9. Step 6：Managed Agent 与 Router

### 目标

让数字员工通过注册接入，而不是继续扩展核心 `Agent` 的条件分支。

### 工作项

- [x] 定义 `AgentSpec`、`ManagedAgent` 和 `AgentRegistry`；
- [x] 建立基于意图、权限、作用域、成本和健康度的 Router；
- [x] 将链接分析升级为第一个 Link Agent；
- [x] 支持结构化 Artifact 和 Agent handoff；
- [x] 注册 Research Agent、Project Assistant 和 Presentation Agent。

### 退出标准

- [x] 新 Agent 可通过注册接入，无需修改协同中枢；
- [x] Agent 的能力、权限、预算和输出均可检查；
- [x] 多 Agent 任务有明确步骤和状态；
- [x] 单个 Agent 失败不会破坏整个会话处理。

## 10. Step 7：治理与私有化生产能力

已建立 Policy Engine、持久审批、脱敏、Secret Provider、Model Gateway、成本配额、审计、metrics、任务级 tracing、数据清理、SQLite 在线备份和私有化部署基线。目标环境的告警后端、容量数值和灾备演练仍属于部署验收，见 [`OPERATIONS.md`](./OPERATIONS.md)。

这一阶段的完成标准不是“功能齐全”，而是权限可解释、运行可追踪、数据可治理、故障可恢复。

## 11. 当前不做

- 不因重构直接拆成微服务；
- 不在 Runtime/Capability 统一前增加多个专业 Agent；
- 不一次性重写 `agent.py`、`memory.py` 或 `url_analyzer.py`；
- 不把真实 LLM 或公网测试放进默认 CI；
- 不用 Prompt 代替确定性的权限和路由规则；
- 不在缺少数据模型和恢复语义时引入复杂异步队列。

## 12. 文档维护规则

- 完成工作项后，在本文件勾选并更新当前阶段；
- 代码行为变化同步更新 `README.md` 和 `CHANGELOG.md`；
- 新技术债先进入 `TECH_DEBT.md`，再决定归属步骤；
- 架构方向变化同步修改 `AI_NATIVE_REFACTORING.md`；
- 每个步骤开始前先确认上一步退出标准，未满足项必须显式记录风险；
- 版本号根据实际交付内容签发，不强行绑定路线图阶段。
