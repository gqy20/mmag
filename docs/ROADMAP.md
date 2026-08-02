# Roadmap

> 状态：Active
>
> 更新时间：2026-08-02
>
> 当前阶段：Deep Agents 原生主链完成，进入身份隔离、Workspace/Sandbox 与企业闭环阶段

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
- [x] LangGraph 作为唯一 checkpoint/HITL Runtime，Deep Agents 作为唯一模型 Harness；
- [x] SQLite checkpoint、稳定 thread ID、原生 interrupt/resume；
- [x] Deep Agents 原生 approve/reject、资格校验、过期、重复恢复与跨进程 graph 重建；
- [x] 删除 Claude Agent SDK、自定义 Anthropic client、手写 LangGraph loop 和运行时双轨。

### Capability 与安全

- [x] 八个内置能力只有一份 `CapabilitySpec`；
- [x] `CapabilityRegistry` 统一内置、MCP 与 Deep Agents Tool 投影；
- [x] Policy 默认拒绝，执行前检查 actor/scope/permission/resource；
- [x] 文件外发和 MCP 副作用进入审批；
- [x] MCP 精确 allowlist、最小 stdio 环境；
- [x] URL 重定向逐跳执行 SSRF 校验。

### Agent 系统与 Package

- [x] `application/`、`agent_system/`、`agent_packages/`、`skill_packages/`、`capabilities/` 职责收口；
- [x] 删除 `agent.py`、`managed_agents.py`、`tools/` 和全局 `prompts.yml`；
- [x] 默认消息主链强制经过 `AgentRouter`；
- [x] 删除 research/project/presentation 的硬编码假 Agent；
- [x] Agent Package 采用扁平 `agents/<name>/agent.yml`，版本只保留在 Manifest；
- [x] `runtime.mode=agent/direct`、AgentFactory 和原子自动注册；旧 Provider Registry 已删除；
- [x] `mmchat` 和 `link` 成为真实 Package，Link 使用确定性 `direct` 模式；
- [x] Capability 根据当前 Package Policy 动态授权，不再绑定全局 Bot Policy；
- [x] Prompt/Schema/eval/Policy/Model Policy 进入 Package Hash 和 provenance；
- [x] Agent Manifest 绑定 Skill 精确版本，Skill Set Hash 进入 Agent 快照；
- [x] 运行时能力收窄为 Agent、Skill 与 Policy 的交集；
- [x] `web-research@1.2.0` 作为可复用 Skill，由 `mmchat@2.2.0` 激活；
- [x] 选中 Skill 投影到 StateBackend，由 Deep Agents SkillsMiddleware 原生渐进披露并随 checkpoint 恢复；
- [x] strict Prompt render、输入/输出/Artifact Schema 和预算强制；
- [x] Model Policy Registry 严格加载并校验 route。

### 结构化输出基线

- [x] Capability 以 JSON Schema 投影为模型工具，Anthropic `tool_use` 参数保持结构化；
- [x] Deep Agents/LangGraph State 累积 messages、files、结构化结果和审批状态；
- [x] `RunRequest.response_schema` 通过 ToolStrategy 生成结构化业务结果，再执行 Skill/Artifact/Agent Schema 与平台 Envelope 校验；
- [x] 删除独立 JSON Provider 和二次 repair loop，结构约束由 Deep Agents response format 统一完成；
- [x] `AgentOutput.result` 是唯一结构化业务结果；`text` 只用于展示，`envelope`、`artifacts` 和 Runtime 状态各自保留明确边界；
- [x] `AgentResult.output` 保存结构化对象，展示层不再从 JSON 文本推断控制状态。
- [x] 删除文本串联式 Handoff；跨 Agent 交接只能使用通过 Schema、版本和 Scope 校验的 Artifact ref；
- [x] 删除裸 Capability handler 注册和隐式 ALLOW Authorizer；所有内置、MCP 与执行平面能力统一经过 `CapabilityExecutor` 和 Policy；
- [x] Runtime Agent 只接受 Package request factory，Agent Package 启动时必须提供 Policy、Model Policy、Skill 和 Execution Profile Registry；

### Mattermost 交付基线

- [x] 当前环境手动探测为 Mattermost Server `11.7.0` Entry，Database/Filestore 正常，`EnableFile=true`、`PluginsEnabled=true`、`AppsPluginEnabled=false`；
- [x] 已支持普通文本/Markdown Post、typing indicator、Thread 内 ack 和经审批的文件发送；
- [x] 文本 Outbox 具备幂等、重试、DLQ 和审计；
- [x] 已确认当前差距：普通最终回复未传 `root_id`，`OutboundMessage` 未承载 `file_ids`/Artifact/action，结构化 Agent 结果尚无 Mattermost 展示层；
- [x] 当前 `MM_URL` 为明文 HTTP，公开配置可读但认证配置探测不安全；未使用 Bot Token 执行探测。

### 评估基线

- [x] Agent/Skill Package 的 contract case 进入各自 Package Hash；
- [x] 定义顶层 `mmag.ai/eval/v1` Profile、Suite、Scenario 和 Result 契约，严格拒绝未知字段与路径越界；
- [x] 实现 `EvaluationRunner`、确定性功能/安全断言、Suite 阈值和原子脱敏 JSON 报告；
- [x] 实现显式 Mattermost 用户登录、Thread 观察、文本审批和 Artifact 交付观察 Driver；真实请求要求
  `external` 标记、双重开关及 HTTPS/本机受信 HTTP；
- [x] 默认测试保持离线，真实用户—Bot smoke、PPT 审批和未授权审批作为显式 Suite；
- [x] 已确认当前边界：尚未把评估执行接入 Package 激活事务，也未持久化正式发布记录。

## 实施原则

1. 单向硬迁移，不保留旧模块转发层；
2. Manifest 负责声明，Schema 负责格式，Policy/Executor 负责安全；
3. 只注册具备 Manifest、Prompt、Schema、Policy、eval 和真实执行器的 Agent；
4. Agent 之间只传 Artifact ref 和严格 Envelope，不猜测自由文本；
5. 模块化单体优先，没有容量或隔离证据不拆微服务；
6. 文件超过 800 行才按稳定职责拆分，避免碎片化；
7. Agent 只输出平台无关的语义结果，展示层负责映射 Mattermost Markdown、Thread、附件和交互组件；
8. 富交互必须基于 Server 版本/配置做能力协商，并始终保留纯 Markdown 和文本命令降级路径。

## 下一步 1：持久化运行 provenance 与预算

优先级：P0。

- [x] 成功消息运行把 Agent/Skill/Prompt/Schema/Policy/Model Policy/eval Hash 写入 AuditEvent；
- [x] 在 AgentRun 原子保存不可变 Package provenance，成功、失败和审批等待共享同一 Run；
- [x] 保存真实模型调用、Capability 调用、token、cost、repair 和 Artifact usage；Provider 未返回价格时 cost 明确为零，不做推测计价；
- [x] 给 `QuotaLedger` 增加 SQLite 原子 reservation/settlement/release，跨进程并发不能超额预占；
- [x] 审批 resume 复用 LangGraph 持久化 Runtime snapshot；DLQ replay 复制并强制校验原始 Package snapshot，不可静默切换版本；
- [x] AuditEvent 记录 Agent route/Model Policy 版本，并为每次 Capability 授权记录脱敏的 Policy decision、rule 和 permission。

退出标准：任意历史 Run 可回答“谁、在什么 scope、使用哪个版本、调用了什么、花费多少、为何允许”，且并发/崩溃不能突破预算。

## 下一步 2：让 Model Policy 驱动 Gateway

优先级：P0。

- [x] 将 `model_class` 映射到平台配置的允许模型集合，Manifest 不能直接指定模型名；
- [x] route/model/max output tokens/temperature 从 Model Policy snapshot 进入实际调用和审计；
- [x] 启动时校验所有已加载 Agent 的 route、model class 和 Provider 参数，不支持的配置直接失败；
- [ ] 为不同 Agent 建立成本、延迟、质量基线。

退出标准：修改 Model Policy 只能通过新版本生效，每次调用都能复现实际模型参数。

## 下一步 3：模型端结构化输出与结果契约

优先级：P0，为专业 Agent、严格 handoff 和展示层提供稳定输入。

- [x] 在 Provider-neutral `RunRequest` 中增加可选的结构化输出声明，包含业务结果 Schema，
  Schema ID/version 和响应策略；在 `AgentResult` 中增加结构化结果，原始文本只用于兼容、诊断和审计；
- [x] 明确区分“模型负责的业务 `result` Schema”和“平台负责的 Envelope Schema”；模型不得生成
  `status`、`usage`、`provenance` 等平台字段，避免把完整 Envelope 误传给模型；
- [x] 使用 Deep Agents `structured_response` 作为唯一结构化出口，不再以 `final_text` 或 JSON 文本作为控制结果；
- [x] 通过 Deep Agents ToolStrategy 约束业务结果，删除文本 JSON parse/repair 分叉；
- [x] 保留 MMAG 的 Draft 2020-12 终局校验作为安全边界，不能因 Provider 声称支持结构化输出而跳过
  Skill、Artifact、Package 和 Envelope 契约校验；
- [x] 结构化结果保持单次 Deep Agents graph；Runtime 出口只按业务 Schema 确定性解码被模型字符串化
  的对象/数组，再进入终局校验，不启动第二次模型运行或 Capability 旁路；
- [x] Registry、Executor 与 Runtime 内部统一传递 `CapabilityResult`，仅在 LangChain ToolMessage、MCP 和交付边界序列化；
- [x] 多轮 LangGraph 和结构修复按实际模型/工具调用累计 token、call count、repair 与错误；Envelope 不再把一次 Runtime 固定计为一次模型调用，当前明确禁用隐式 Provider fallback；
- [ ] 覆盖 Provider-native、Tool Strategy、文本 fallback、非法 JSON、Schema 不匹配、重复结构化结果、
  repair 失败、工具副作用保留和 checkpoint resume 测试；默认测试使用离线 Fake Backend。

退出标准：启用结构化输出的 Agent 在 Runtime 边界返回已验证对象；Provider 能力变化只改变生成策略，
不改变业务契约；格式错误不会重放工具副作用，Artifact、usage 和 provenance 不丢失；下游不再从自然语言
或 JSON 文本猜测业务结果。

## 下一步 4：执行 eval 发布门禁

优先级：P0。

- [x] 建立版本化系统 Eval Loader、Runner、真实 Mattermost Driver、确定性断言和报告格式；
- [x] Loader/Registry 各自负责 Schema、治理 provenance 和 Capability 声明；Activation Gate 不重复校验，只执行可选 contract case 并登记已验证 staging 批次；
- [ ] quality case 定义数据集版本、阈值和评分器版本；
- [x] Package release 只记录 Package/eval Hash、Gate 版本、检查项、发布时间和发布主体；真实 EvaluationRun 产生后再记录结果与环境 Hash；
- [x] Gate 先验证完整 staging 批次，再用单一 SQLite 事务发布，失败 Package 不生成 release 记录或进入 Registry；
- [ ] 旧制品可按 Package Hash 原子回滚。

退出标准：坏 Prompt、坏 Schema、越权能力和质量回退不能进入新 Run。

## 下一步 5：Mattermost 响应展示与交互层

优先级：P1，作为 Research、Presentation、审批和 Artifact 交付的公共基础。

- [x] 定义平台无关的 `ResponseView` 契约，统一表达 `kind`、`title`、`summary`、`sections`、`sources`、`warnings`、`artifacts`、`actions` 和 Run 状态；
- [x] 实现 `ResponsePresenter`，将 `AgentOutput.result/artifacts/envelope` 转为 `ResponseView`，禁止将结构化 JSON 直接展示给默认用户；
- [x] 实现 `MattermostRenderer`，将 `ResponseView` 确定性地映射为 Markdown、`props.attachments`、文件附件和 action，对模型/外部内容做长度限制、安全清洗与安全链接处理；
- [x] 扩展 `OutboundMessage` 和 SQLite Outbox，持久化 `root_id`、`message_kind`、`artifact_refs/file_ids`、`props`、`actions`、`update_post_id` 及稳定幂等键；
- [x] 统一 Thread 策略：ack、进度、审批、附件、错误和最终结果均继承原始 `root_id`，避免在频道中散落；
- [x] 将 `get` ack 替换为可配置的简短确认；短任务仅使用 typing，长任务创建单个状态 Post 并原地更新，阶段必须来自真实 Lifecycle Event，不伪造进度百分比或 ETA；
- [x] 接通 Deep Agents/LangChain 文本增量事件，对模型 Agent 节流更新单一 Post，最终结果仍经 ResponseView + Outbox 覆盖交付；
- [x] 实现 Markdown-aware 长消息分段，保留标题、链接、代码围栏和序号；详细报告默认“Thread 摘要 + Artifact 附件”；
- [x] 将 `send_file` 的直接上传/发帖副作用收口到 Delivery/Outbox：执行层只产出 Artifact ref，交付层在审批后上传、绑定 `file_ids` 并重试；
- [x] 为 Link/Research 结果提供稳定 Presenter，在 Thread 中展示结论、关键字段、来源和警告，完整 JSON 仅作为可下载 Artifact 或审计数据；
- [ ] 实现签名、短时、一次性 Action Token 和 Callback Endpoint，支持批准/拒绝/重试/下载/返工，每次回调重新校验 actor、scope、资源和当前状态；
  - 已完成批准/拒绝及文本降级；Delivery 手工重试、授权下载与创建新 Run 的返工仍需各自业务状态服务，不能用按钮伪造完成。
- [x] 交互按钮优先使用 Mattermost `props.attachments`/action，不支持或回调不可达时降级为 `批准 <id>` / `拒绝 <id>` 文本命令；
- [x] 实现统一错误展示分类：输入问题、权限不足、等待审批、资源耗尽、执行超时、外部依赖失败和系统故障；对用户隐藏秘密/堆栈，保留可查询 Run ID；
- [ ] 增加可重复的 Mattermost 能力探测，记录 Server 版本、Edition、文件/插件/交互开关和客户端兼容矩阵；认证级探测必须经 HTTPS 或本机受信通道；
  - 已完成受信传输限制和 Server/Edition/文件/插件/交互开关审计；目标版本上的 Web/Desktop/Mobile 兼容矩阵仍待验收。
- [ ] 覆盖 Web/Desktop/Mobile 的 Markdown、Thread、分段、重试、回调幂等、按钮降级和附件失败测试。

退出标准：任何 Agent 都只产出平台无关结果；Mattermost 用户在一个 Thread 中获得可读摘要、真实进度、来源、可下载 Artifact 和可审计操作；富交互不可用时仍能用 Markdown/文本命令完成同一流程。

## 下一步 6：Research Package

优先级：P1。

- [x] 定义 `report` Manifest、只读 Policy、Prompt 和输入/输出 Schema；
- [x] `report@1.3.0` Skill 将结果升级为证据账本式结构化输出，并按需使用受治理 MCP；
- [x] 定义 `research_report` Artifact Schema；
- [ ] 接入来源去重、时效性和证据覆盖 eval；
- [ ] 将报告持久化到 Artifact Repository；
- [ ] 覆盖取消、超时、部分来源失败和预算耗尽。

退出标准：Research 只能读允许来源，输出始终是可验证、有来源、有版本的 Artifact。

## 下一步 7：受控 Python/CLI 执行平面

优先级：P1，为 Presentation 等生成型 Agent 提供基础能力。

- [x] 定义版本化 `ExecutionProfile` YAML、Schema 与 Registry，将 runner、运行时摘要、固定可执行文件、环境变量、文件系统、网络和资源限制纳入 Hash/provenance；
- [x] 实现通用 `ProcessRunner` 和受管 `ScriptExecutor`，只使用固定 argv 启动已注册的可执行文件，禁止 `shell=True`、任意命令字符串和动态 `eval/exec`；
- [x] 执行前校验 Skill `resources.scripts` 声明、Package provenance 和脚本 SHA-256；Skill 脚本不可作为普通可披露资源；
- [ ] 生产 Runner 为每个 Run 建立真实文件系统边界；当前 PPT Demo 从独立 tmp 启动，但完整宿主 Shell 可越出工作区；
- [x] 强制超时、CPU、内存、进程数、文件/输出大小并清空父进程环境；当前 PPT Demo 按明确授权保留宿主网络；
- [x] 将 Agent `capabilities.allow/deny` 与 `execution_profiles`、Skill `required/optional_capabilities` 与 Profile、Package Policy、Execution Profile command 计算为最终权限交集；Manifest 只能缩小权限；
- [x] 注册高层 `ppt.build` Capability，内部固定编排 source/render/preview；诊断执行统一进入受治理 Workspace；
- [x] 将命令摘要、Profile/脚本/可执行文件 Hash、返回码、观测字节/时长、限制、错误码和产物 Hash 写入结构化审计，日志不记录秘密和完整输入；
- [x] 成功输出原子提交到 Artifact Repository，失败/取消/超时清理 staging，异常遗留临时目录按 retention 回收；
- [x] 添加命令注入、非法 runner/环境、符号链接、超限产物、篡改脚本、缺失 sandbox、Manifest 自授权和跨 scope Artifact 等负向测试；宿主 Shell 风险作为 Demo 例外显式记录。

实现与部署前提见 [受控执行平面](EXECUTION.md)。当前开发环境已完成 PPTX/PNG smoke；宿主 Shell 是 Demo 例外，生产发布前必须换回真正隔离的 Runner 并补安全负向验收。

退出标准尚未满足：生产 Runner 必须禁止任意命令、越界文件访问和未授权网络。当前本地完整 Shell 仍是显式 Demo 例外，但已统一到可替换 Backend，不再存在 PPT 专用旁路。

## 下一步 8：Presentation Package 与严格 handoff

优先级：P1，依赖下一步 5、6、7。

- [ ] Presentation 只接受 `research-report` ref；
- [ ] 输入 Artifact 在执行前校验版本和 scope；
- [x] 定义 `slides@2.2.0` Skill、受控 Markdown、注册主题、PptxGenJS 原生对象渲染、严格 Presentation Bundle 契约与默认 PPTX 审批交付；
- [x] 定义 `ppt@2.2.0` Agent Manifest 和默认拒绝 Policy，PPTX 默认进入审批交付；
- [x] Presentation Package `ppt@3.1.1` 绑定 `ppt@2.2.0` Execution Profile、锁定的 PptxGenJS bundle、高层 `ppt.build` 与受治理 Workspace；
- [x] 通过执行平面输出规范化 Markdown、可编辑 PPTX 与直接 PNG 预览 Artifact；
- [x] 同一份 Markdown/Theme 生成 PPTX 与首页 PNG，不再以 LibreOffice/PDF 作为预览前置；
- [x] PPTX 默认经过 LangGraph 审批和 Outbox 交付；
- [ ] handoff 每一步持久化状态、失败、重试和成本。

退出标准：Research → Presentation 不传自由文本；非法或越权 Artifact 不能进入下游；PPT 生成不依赖宿主机通用 Shell/Python 权限。

## 下一步 9：反馈、返工与业务闭环

优先级：P1。

- [ ] 用户可接受、驳回或请求返工；
- [ ] 返工创建新 Run，关联原 Artifact，不覆盖历史；
- [ ] 反馈进入质量数据集但默认不进入 Prompt；
- [ ] Task 只有在交付被接受或明确终止后才闭环；
- [ ] 建立任务、AgentRun、Artifact、Delivery、Feedback 的端到端报表。

退出标准：企业任务从请求到交付、审批、验收、返工和审计形成可查询闭环。

## 下一步 10：结构化可观测性与部署验收

优先级：P1。

### 当前审计基线

- [x] 已有统一 `mmag.*` Logger、控制台/文件输出、启动期保留清理和基于 `ContextVar` 的请求
  `trace_id`；
- [x] Capability 常规日志只记录参数 key、输入 Hash、耗时和结果大小，受控执行审计已记录 Profile、
  脚本、可执行文件、argv、输入和 Artifact Hash；
- [x] 已有独立 `AuditEvent` 存储，覆盖 Agent 成功运行、受控执行、审批、Inbox 重试/replay、Delivery
  终态和 Mattermost 能力探测；
- [x] 中心化 Redaction Filter、安全异常格式和内容无关的 Deep Agents Callback 已落地，普通日志不再记录消息片段、附件名、URL query、完整 Tool 参数和 Provider 异常正文；
- [x] 可嵌套、自动 reset 的 `LogContext` 已取代手工 trace 前缀，并贯穿 Agent、Deep Agents、模型和 Capability 主链；Delivery/后台任务的全链验收继续保留；
- [x] 普通日志已有版本化事件名与 JSON Lines；Agent 成功/失败、模型调用和 LangGraph Tool 调用已写入 AuditEvent；Policy 全量决策仍待补齐；
- [x] AuditEvent Python 模型已暴露 `created_at`，查询支持 trace/run/actor/scope/decision/时间游标；事件 Schema 全覆盖、归档、导出、访问控制和防篡改仍待完成；
- [x] 空 `LOG_DIR`、大小轮转、UTC、PID 文件隔离和 Handler 关闭已修复；过期清理仍只在启动执行，多进程聚合交给部署日志平台；
- [x] 已覆盖 LogContext 并发隔离/恢复、Secret/URL/异常脱敏，以及 Deep Agents model/tool 内容无关审计；完整部署验收继续保留。

### 实施批次

1. 日志安全基线（P0）：
   - [x] 删除消息正文、URL query、敏感文件名和未经分类的异常正文日志；正文只允许进入受保护、显式授权的
     业务存储，不能进入普通 telemetry；
   - [x] 实现中心化 Redaction Filter 和安全异常序列化，默认只输出稳定 `error_code`、异常类型和允许公开的
     摘要；
   - [x] 配置日志改为非敏感字段 allowlist 或带敏感元数据的声明式过滤；新增 Secret 不得依赖手工维护
     denylist 才能避免泄漏；
   - [x] 为正文、Token、Authorization header、签名 URL、Tool 参数和 Provider 异常增加负向泄漏测试。

2. 结构化上下文与运行日志（P1）：
   - [x] 用可绑定、可嵌套并通过 ContextVar token 自动 reset 的 `LogContext` 取代人工 `trace.prefix()`；
   - [ ] 统一传播 `trace_id`、`task_id`、`run_id`、`conversation_id`、`agent_ref`、`skill_ref`、
     `capability`、`policy_ref` 和 `delivery_id`，包括分区 worker、流式投影、Outbox、审批 interrupt/resume
     与后台任务；
   - [x] 定义版本化运行事件契约，至少包含 UTC timestamp、event、level、status、duration_ms、error_code、
     attempt 和受控 provenance 字段；
   - [x] 生产环境输出 JSON Lines，开发环境保留人类可读 Formatter；业务代码使用稳定事件名，不依赖中文
     自由文本和 Emoji 作为机器查询条件。

3. 审计闭环（P1）：
   - [ ] 为 Agent 成功/失败/超时、Runtime、每次 CapabilityCall、Policy allow/deny/approval、模型调用、
     Approval、Execution、Artifact、Inbox 和 Delivery 建立统一事件目录与 details Schema；
   - [ ] 当前 LangGraph Callback 已持久记录调用 ID、run/trace、能力名、Policy 决策、状态、耗时和安全输入摘要；后续将分阶段 AuditEvent 收敛为具有状态约束的 `CapabilityCall` 实体生命周期；
   - [ ] 所有审计事件记录 `schema_version`、`created_at`、actor/scope/trace/run 和 Package/Prompt/Skill/
     Policy/Model Policy provenance；失败分支与恢复分支不得漏记终态；
   - [ ] 扩展审计查询，支持 trace/run/actor/scope/event/time/status、游标分页和企业导出；定义保留、归档、
     访问控制及可选防篡改/外部归档边界；
   - [ ] 审计写入失败必须产生可告警的降级事件；高风险副作用按策略决定 fail-close，不能统一静默继续。

4. Metrics、Trace 与日志运维（P1）：
   - [ ] 输出 Agent/Skill/Capability/Approval/Delivery 的 duration、status、error code、queue depth、retry、
     token 和 cost 指标，限制 actor、run、trace 等高基数字段进入 metrics label；
   - [ ] 可选接入 OpenTelemetry，复用统一 trace/run 上下文；默认不采集 Prompt、消息正文、工具参数、
     stdout/stderr 或 Artifact 内容；
   - [ ] 容器部署默认写 stdout 交给平台采集；本地文件模式修复禁用语义，并实现安全轮转、UTC 时区、
     Handler 关闭及多实例边界；
   - [ ] 建立 Inbox/Outbox 失败、Runtime timeout/rate-limit、审批积压、预算耗尽、WebSocket 重连、数据库
     磁盘和 Sandbox/Artifact 故障告警。

5. 部署验收（P1）：
   - [ ] 完成结构化字段契约、异步 Context 隔离/恢复、审计完整性、日志轮转和 Secret 泄漏测试；
   - [ ] 在目标日志平台验证按 Run/Package/Capability 定位一次完整请求，并验证采样、脱敏、保留和权限；
   - [ ] 完成目标环境容量、备份恢复、升级回滚和灾难演练，记录可验证 RPO/RTO。

退出标准：任一请求可通过稳定 trace/run 关联 Agent、Skill、Policy、Capability、审批、Artifact 和 Delivery；
普通 telemetry 不包含 Secret、消息正文或未脱敏工具参数；关键失败有指标和告警；审计可按 scope 查询并满足
保留策略；目标环境有可验证 RPO/RTO。

## 下一步 11：Deep Agents 原生化与可替换执行 Backend

优先级：P1；完整方案见 [Deep Agents 原生化重构方案](DEEP_AGENTS_REFACTORING.md)，决策边界见
[ADR-0007](adr/0007-deepagents-sandbox-runtime.md)。核心 Harness 已完成，剩余工作集中在通用 Workspace/Sandbox。

- [x] Agent Manifest 删除 `text-v1/json-v1/single-v1` Provider，运行方式收敛为默认 `agent` 与显式
  `direct`；
- [x] 实现 `DeepAgentRuntime`，复用统一 `RunRequest`、`AgentResult`、SQLite checkpointer、稳定
  thread ID、deadline、usage、response format 和 interrupt；
- [x] 用受 Model Policy 管理的 `BaseChatModel` 替换自定义 Anthropic Agent loop；
- [x] 将 `CapabilitySpec` 投影成 LangChain Tool，继续经过同一个 `CapabilityExecutor`、动态 Policy、
  审批和审计；
- [x] 使用 LangChain 原生 `ModelCallLimitMiddleware`、`ToolCallLimitMiddleware` 强制每个 Run/thread 的
  模型与全部 Tool 调用次数上限，审批恢复不重置计数；美元/token 原子预算仍归入预算阶段；
- [x] 实现可信单 Skill 投影，接入 SkillsMiddleware，禁用默认通用 Subagent；
- [x] 用 Deep Agents `FilesystemPermission` 将 Skill 限定为只读、`/workspace/**` 可读写、其他路径默认
  拒绝；只有绑定 Workspace Capability 与 Execution Profile 的 Agent 才获得原生 `execute`；
- [x] 实现 `GovernedWorkspaceBackend` 与 `workspace.read/write/execute` canonical Capability；
- [x] 接入显式危险的 `LocalExecutionBackend`，复用稳定 Run Workspace、Execution Profile 超时、最小
  环境、输出限制、结构化日志与 TTL 回收；关闭危险开关时失败关闭；
- [x] 让 PPT Agent 在真实 Workspace 中执行、通过 `workspace.commit` 幂等登记 Profile 固定输出，并删除
  `ppt.shell`；Mattermost 真实上传继续由外部测试确认；
- [x] 把 Deep Agents messages/interrupt 映射为 Mattermost 文本流与审批事件；Tool/Artifact 细粒度展示继续归入可观测性阶段；
- [x] 全部 Agent 迁移后删除手写 LangGraph loop、旧 Provider、Claude SDK 并行路径、旧 Skill 资源
  加载和相关兼容配置；
- [ ] 有生产预算后新增一个 `RemoteSandboxBackend`，不修改 Agent/Skill Manifest 即可替换本地执行；
- [ ] 完成越权文件、Secret 泄漏、重复恢复、超限输出、跨 Run 访问和异常回收的生产门禁。

退出标准：Deep Agents 是模型驱动 Agent 的唯一 Harness，LangGraph 是唯一 checkpoint/HITL Runtime；
确定性 Agent 使用 `direct`；PPT 可通过真实 Workspace 形成闭环；本地完整 Shell 被明确标记为非
Sandbox；切换远程 Sandbox 只替换 Execution Backend。

## 下一步 12：Mattermost 身份、Scope 与个人空间隔离

优先级：P0；这是个人工作台、多用户长期记忆和企业多租户上线的前置条件。

### 当前边界

- [x] 入站消息以 Mattermost `post.user_id` 作为可信 `actor_id`，并贯穿 Agent Run、Capability、审批和审计；
- [x] 频道消息、摘要和团队知识按 `channel_id` 组织，Artifact 读取要求精确匹配当前 `scope_id`；
- [x] 审批回调使用签名、短时、一次性 Token，并在执行前重新检查审批资格；
- [x] Scope 已包含稳定的 Installation、Tenant、Scope Kind 和个人 Owner；DM 使用 Personal Scope，
  O/P/G 使用 Channel Scope，不再以 Team 充当租户；
- [x] 用户画像以及现有消息、知识、摘要和 URL 缓存查询已按 Installation + Tenant 分区；
- [x] 普通频道不再注入或投影当前用户私人画像；
- [ ] 个人案例、个人长期记忆和 Skill 草稿还没有独立 PersonalSpace；消息编辑/删除、成员退出和用户停用
  尚未同步清理上下文、索引和后台任务权限；
- [x] LangGraph Checkpoint 恢复强制匹配原 actor、scope、installation 和 tenant；
- [ ] 本地完整 Shell 是 Demo 例外，Run 目录约定不能构成生产级文件系统隔离。

### 实施内容

- [x] 增加稳定的 `MM_INSTALLATION_ID` 与 `MM_TENANT_ID`，建立可信 `Principal` 和类型化 `Scope`；
  身份与 Scope 只能由服务端根据认证事件派生，模型、Manifest 和工具参数不能指定；
- [x] 正确识别 Mattermost `O/P/D/G`：Bot DM 进入个人模式，公开频道、私有频道和 GM 进入共享模式；
  修复配置 `MM_TEAM_ID` 后 DM/GM 被拒绝的问题；
- [x] 当前个人模式可加载本人画像和 DM 上下文；共享模式只加载频道/项目上下文，不注入或投影私人画像；
- [x] 将用户画像升级为 Installation + Tenant + User 联合身份，现有 Memory Repository 查询固定绑定
  当前 Installation/Tenant；
- [ ] 新增 PersonalSpace、WorkCase、PersonalMemory 和 SkillDraft，并让群聊私人操作转入 DM 或私有交付；
- [x] 为现有 SQLite FTS、缓存、Artifact 和 Checkpoint 增加 Tenant/Scope 查询或终局校验；
- [ ] 后续向量索引必须使用相同 Scope 分区，不能先全库召回再在应用层过滤；
- [ ] Bot Token 只代表服务身份，不继承为发消息用户的权限；读取、恢复、审批、分享和交付前通过
  Mattermost 成员关系/角色及 MMAG Policy 做动态影子授权，授权查询失败时默认拒绝；
- [ ] 消费 `post_edited`、`post_deleted`、成员关系和用户状态事件，及时更新或撤销消息、摘要、索引、
  缓存与后台任务权限；MMAG 保留策略不得绕过 Mattermost/企业数据保留要求；
- [ ] 个人 Artifact 默认只交付到本人 DM；发布到项目或频道时创建带 provenance 的新版本并显式审批，
  不原地放宽原对象 Scope；
- [ ] 覆盖跨用户 WorkCase/Artifact/检索、DM 空 `team_id`、退出私有频道、删除消息、伪造 actor/scope、
  Checkpoint 越权恢复和个人结果误投公共频道等负向验收。

退出标准：同一 Mattermost 实例中的任意两个用户、频道和企业租户不能通过消息上下文、检索、Artifact、
Checkpoint、审批或执行目录越界读取彼此数据；权限撤销和源消息删除可传播；群聊只使用共享上下文，个人
上下文只在受控个人模式中使用。

## 目标业务场景

以下场景先作为产品与架构对接目标，具体交互、数据来源和验收用例后续分别细化。

### 场景一：个人工作台与 Skill 沉淀

用户在 Bot DM 中调用受允许的 Skill 完成典型工作，把输入、过程、结果和反馈沉淀为私有 WorkCase，并可
生成个人 Skill 草稿。当前 Agent/Skill/Artifact 基础可复用，但必须先完成 PersonalSpace、个人检索隔离、
显式发布和生产 Sandbox。

### 场景二：基于历史工作的个人数字人

用户显式选择可使用的历史消息、文档、案例和回答边界，形成带来源、保留策略和撤销能力的个人数字人；
面向他人回答时明确数字人身份，对高风险承诺或外发内容请求本人确认。当前用户画像较浅，仍缺授权采集、
私有知识索引、代答策略和持续反馈闭环。

### 场景三：多人群聊中的主动总结与回答

Agent 在公开/私有频道或 GM 中按触发规则主动接入，使用当前频道有权访问的消息完成会议总结、问题回答和
待办提取，不读取任何成员的私人空间。当前频道上下文、摘要、Thread 和展示能力已具备基础，仍需补充主动
触发治理、成员变更同步、来源可追溯和会议结果沉淀。

### 场景四：业务提交、辅助判断与真人审批

下级提交结构化业务材料后，Agent 完成完整性检查、前置总结、风险提示和建议，再通过 Mattermost 交互组件
请求具备资格的真人作最终决定；决定、理由、返工、Artifact 和业务状态全程可审计。当前 LangGraph HITL、
按钮审批、Artifact 和审计已有基础，仍缺业务对象 Schema、组织审批关系、条件化审批链和验收/返工终态闭环。

## 当前明确不做

- 不恢复旧 Agent、Tool 或 Prompt 兼容入口；
- 不为了“多 Agent”数量重新注册没有 Package 契约的占位 Agent；
- 不用 Prompt 代替权限、审批、幂等和预算；
- 不向普通业务 Agent 暴露宿主机通用 Shell 或动态 Python；Demo 本地完整 Shell 只对显式授权的
  专用 Package、可信用户和危险开关开放，禁止进入公网或多租户生产环境；
- 不允许 Agent/Skill Manifest 自授权或放宽平台执行配置；
- 不让 Agent Prompt 直接生成 Mattermost `props`、action callback 或平台专用协议；
- 不把富卡片/按钮作为唯一交互路径，不支持时必须降级为 Markdown/文本命令；
- 不在明文 HTTP 连接上传输 Bot Token 做管理级配置探测；
- 不在 Artifact/Scope/恢复语义未完成前拆微服务；
- 不把真实公网或 LLM 测试放进默认 CI。
