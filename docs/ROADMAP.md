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

- [x] `application/`、`agent_system/`、`agent_packages/`、`skill_packages/`、`capabilities/` 职责收口；
- [x] 删除 `agent.py`、`managed_agents.py`、`tools/` 和全局 `prompts.yml`；
- [x] 默认消息主链强制经过 `AgentRouter`；
- [x] 删除 research/project/presentation 的硬编码假 Agent；
- [x] Agent Package 采用扁平 `agents/<name>/agent.yml`，版本只保留在 Manifest；
- [x] `execution/routing`、可信 Provider Registry、AgentFactory 和原子自动注册；
- [x] `mmchat` 和 `link` 成为真实 Package，Link 使用通用 Capability Provider；
- [x] Capability 根据当前 Package Policy 动态授权，不再绑定全局 Bot Policy；
- [x] Prompt/Schema/eval/Policy/Model Policy 进入 Package Hash 和 provenance；
- [x] Agent Manifest 绑定 Skill 精确版本，Skill Set Hash 进入 Agent 快照；
- [x] 运行时能力收窄为 Agent、Skill 与 Policy 的交集；
- [x] `web-research@1.0.0` 作为首个可复用 Skill，由 `mmchat@1.2.0` 激活；
- [x] Skill 三级渐进式披露、Resource Hash 缓存、运行预算和审批 resume 恢复；
- [x] strict Prompt render、输入/输出/Artifact Schema 和预算强制；
- [x] Model Policy Registry 严格加载并校验 route。

### 结构化输出基线

- [x] Capability 以 JSON Schema 投影为模型工具，Anthropic `tool_use` 参数保持结构化；
- [x] LangGraph 使用类型化 State 累积消息、Artifact、Capability 调用和审批状态；
- [x] `ppt`、`project`、`report` 通过 `langgraph/json-v1` 产出业务结果，运行后执行 JSON 解析、
  Skill/Artifact Schema 校验、平台 Envelope 组装和 Draft 2020-12 终局校验；
- [x] 无效结果最多触发一次禁用 Capability 的结构修复，二次失败以稳定错误结束；
- [x] `AgentOutput` 已区分 `text`、`structured_result`、`envelope`、`artifacts` 和 Runtime 结果；
- [x] 已确认当前边界：输出 Schema 尚未进入 `RunRequest` 和模型 API，LangGraph 最终状态仍是
  `final_text`；现有能力属于提示词生成 JSON 后的校验与修复，不是 Provider/LangGraph 模型端强约束。

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
- [ ] 在 AgentRun 原子保存完整 provenance，并覆盖失败、审批 resume 和 replay；
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

## 下一步 3：模型端结构化输出与结果契约

优先级：P0，为专业 Agent、严格 handoff 和展示层提供稳定输入。

- [ ] 在 Provider-neutral `RunRequest` 中增加可选的结构化输出声明，至少包含业务结果 Schema、
  Schema ID/version 和响应策略；在 `AgentResult` 中增加结构化结果，原始文本只用于兼容、诊断和审计；
- [ ] 明确区分“模型负责的业务 `result` Schema”和“平台负责的 Envelope Schema”；模型不得生成
  `status`、`usage`、`provenance` 等平台字段，避免把完整 Envelope 误传给模型；
- [ ] 在 LangGraph State 中增加 `structured_result`、`validation_errors` 和 `repair_count`，将现有
  Agent/Capability 循环的最终出口路由到无副作用的 `finalize` 节点，不再以 `final_text` 作为 JSON
  Agent 的唯一结果；
- [ ] 扩展模型 Backend：Provider 支持时使用原生 Schema-constrained output；不支持时使用强制的
  final-output Tool Strategy；仅在两者不可用时降级到当前文本 JSON parse/validate/repair；
- [ ] 保留 MMAG 的 Draft 2020-12 终局校验作为安全边界，不能因 Provider 声称支持结构化输出而跳过
  Skill、Artifact、Package 和 Envelope 契约校验；
- [ ] repair 使用相同的结构化输出约束且最多一次，只修复业务结果，不重新开放 Capability 或重放
  外部副作用；保留首次运行的 Artifact、Capability 调用、审批、token、cost 和 provenance；
- [ ] 统一内部 Capability 结果类型，避免在 Registry 与 LangGraph 之间反复执行对象 → JSON 字符串 →
  对象转换；只在模型 Provider、MCP 和交付边界序列化；
- [ ] 让多轮 LangGraph、结构修复和 Provider fallback 的实际模型调用、token、cost 与错误进入统一
  usage 和审计，不再把一次 Runtime 执行固定计为一次模型调用；
- [ ] 覆盖 Provider-native、Tool Strategy、文本 fallback、非法 JSON、Schema 不匹配、重复结构化结果、
  repair 失败、工具副作用保留和 checkpoint resume 测试；默认测试使用离线 Fake Backend。

退出标准：启用结构化输出的 Agent 在 Runtime 边界返回已验证对象；Provider 能力变化只改变生成策略，
不改变业务契约；格式错误不会重放工具副作用，Artifact、usage 和 provenance 不丢失；下游不再从自然语言
或 JSON 文本猜测业务结果。

## 下一步 4：执行 eval 发布门禁

优先级：P0。

- [x] 建立版本化系统 Eval Loader、Runner、真实 Mattermost Driver、确定性断言和报告格式；
- [ ] Agent/Skill contract case 在激活前离线执行；
- [ ] quality case 定义数据集版本、阈值和评分器版本；
- [ ] EvaluationRun、结果 Hash、环境快照、发布时间和发布人进入控制面发布记录；
- [ ] 失败 Package 不得生成发布制品或进入部署；
- [ ] 旧制品可按 Package Hash 原子回滚。

退出标准：坏 Prompt、坏 Schema、越权能力和质量回退不能进入新 Run。

## 下一步 5：Mattermost 响应展示与交互层

优先级：P1，作为 Research、Presentation、审批和 Artifact 交付的公共基础。

- [x] 定义平台无关的 `ResponseView` 契约，统一表达 `kind`、`title`、`summary`、`sections`、`sources`、`warnings`、`artifacts`、`actions` 和 Run 状态；
- [x] 实现 `ResponsePresenter`，将 `AgentOutput.structured_result/artifacts/envelope` 转为 `ResponseView`，禁止将结构化 JSON 直接展示给默认用户；
- [x] 实现 `MattermostRenderer`，将 `ResponseView` 确定性地映射为 Markdown、`props.attachments`、文件附件和 action，对模型/外部内容做长度限制、安全清洗与安全链接处理；
- [x] 扩展 `OutboundMessage` 和 SQLite Outbox，持久化 `root_id`、`message_kind`、`artifact_refs/file_ids`、`props`、`actions`、`update_post_id` 及稳定幂等键；
- [x] 统一 Thread 策略：ack、进度、审批、附件、错误和最终结果均继承原始 `root_id`，避免在频道中散落；
- [x] 将 `get` ack 替换为可配置的简短确认；短任务仅使用 typing，长任务创建单个状态 Post 并原地更新，阶段必须来自真实 Lifecycle Event，不伪造进度百分比或 ETA；
- [x] 接通 LangGraph/Anthropic 文本增量事件，对 `text-v1` Agent 节流更新单一 Post，最终结果仍经 ResponseView + Outbox 覆盖交付；
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
- [x] 新增 `report@1.0.0` Skill，将结果升级为证据账本式结构化输出；
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
- [x] 注册高层 `ppt.build` Capability，内部固定编排 source/render/preview；Demo 阶段另以显式 `ppt.shell` Capability 开放宿主 Shell，Policy 与审计独立；
- [x] 将命令摘要、Profile/脚本/可执行文件 Hash、返回码、观测字节/时长、限制、错误码和产物 Hash 写入结构化审计，日志不记录秘密和完整输入；
- [x] 成功输出原子提交到 Artifact Repository，失败/取消/超时清理 staging，异常遗留临时目录按 retention 回收；
- [x] 添加命令注入、非法 runner/环境、符号链接、超限产物、篡改脚本、缺失 sandbox、Manifest 自授权和跨 scope Artifact 等负向测试；宿主 Shell 风险作为 Demo 例外显式记录。

实现与部署前提见 [受控执行平面](EXECUTION.md)。当前开发环境已完成 PPTX/PNG smoke；宿主 Shell 是 Demo 例外，生产发布前必须换回真正隔离的 Runner 并补安全负向验收。

退出标准尚未满足：生产 Runner 必须重新禁止任意命令、越界文件访问和未授权网络。当前已实现 Agent/Skill/Policy/Profile 权限交集、Artifact 与审计链路，但 `ppt.shell` 是显式 Demo 例外。

## 下一步 8：Presentation Package 与严格 handoff

优先级：P1，依赖下一步 5、6、7。

- [ ] Presentation 只接受 `research-report` ref；
- [ ] 输入 Artifact 在执行前校验版本和 scope；
- [x] 定义 `slides@2.2.0` Skill、受控 Markdown、注册主题、PptxGenJS 原生对象渲染、严格 Presentation Bundle 契约与默认 PPTX 审批交付；
- [x] 定义 `ppt@2.2.0` Agent Manifest 和默认拒绝 Policy，PPTX 默认进入审批交付；
- [x] Presentation Package `ppt@2.2.0` 绑定 `ppt@2.1.0` Execution Profile、锁定的 PptxGenJS bundle、高层 `ppt.build` 与 Demo `ppt.shell`；
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
- [x] 已确认敏感数据风险：用户消息片段进入 INFO，URL 和附件名称可能进入日志，第三方异常正文未经
  统一脱敏；当前没有中心化 Redaction Filter；
- [x] 已确认关联性缺口：Trace 仍通过人工拼接文本前缀，异步 Pipeline、Delivery、审批恢复及部分审计
  没有稳定携带同一组 task/run/trace 标识，异常路径也不能保证自动恢复 Context；
- [x] 已确认结构化缺口：普通日志没有稳定事件名和 JSON 字段；CapabilityCall 生命周期未逐次持久化，
  通用 Policy、模型调用和 Agent 失败审计不完整；
- [x] 已确认审计查询缺口：AuditEvent 没有事件 Schema 版本，Python 模型未暴露 `created_at`，查询缺少
  trace/run/actor/scope/时间区间和分页能力，也没有归档、保留及防篡改策略；
- [x] 已确认运维缺口：空 `LOG_DIR` 不能真正关闭文件输出，文件没有大小轮转，过期清理只在启动时执行，
  时间戳没有时区，多实例可能竞争同一启动日志文件；
- [x] 已确认测试缺口：当前只覆盖 TraceContext 并发隔离，没有日志脱敏、Context 异常恢复、结构化字段、
  Handler 生命周期和审计完整性负向测试。

### 实施批次

1. 日志安全基线（P0）：
   - [ ] 删除消息正文、URL query、敏感文件名和未经分类的异常正文日志；正文只允许进入受保护、显式授权的
     业务存储，不能进入普通 telemetry；
   - [ ] 实现中心化 Redaction Filter 和安全异常序列化，默认只输出稳定 `error_code`、异常类型和允许公开的
     摘要；
   - [ ] 配置日志改为非敏感字段 allowlist 或带敏感元数据的声明式过滤；新增 Secret 不得依赖手工维护
     denylist 才能避免泄漏；
   - [ ] 为正文、Token、Authorization header、签名 URL、Tool 参数和 Provider 异常增加负向泄漏测试。

2. 结构化上下文与运行日志（P1）：
   - [ ] 用可绑定、可嵌套并通过 ContextVar token 自动 reset 的 `LogContext` 取代人工 `trace.prefix()`；
   - [ ] 统一传播 `trace_id`、`task_id`、`run_id`、`conversation_id`、`agent_ref`、`skill_ref`、
     `capability`、`policy_ref` 和 `delivery_id`，包括分区 worker、流式投影、Outbox、审批 interrupt/resume
     与后台任务；
   - [ ] 定义版本化运行事件契约，至少包含 UTC timestamp、event、level、status、duration_ms、error_code、
     attempt 和受控 provenance 字段；
   - [ ] 生产环境输出 JSON Lines，开发环境保留人类可读 Formatter；业务代码使用稳定事件名，不依赖中文
     自由文本和 Emoji 作为机器查询条件。

3. 审计闭环（P1）：
   - [ ] 为 Agent 成功/失败/超时、Runtime、每次 CapabilityCall、Policy allow/deny/approval、模型调用、
     Approval、Execution、Artifact、Inbox 和 Delivery 建立统一事件目录与 details Schema；
   - [ ] 将 LangGraph 工具调用接入持久 `CapabilityCall` 生命周期，记录调用 ID、run/trace、能力名、授权
     决策、状态、耗时和安全输入摘要，不记录完整输入输出；
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

## 下一步 11：可选 Deep Agents Runtime 与可替换 Sandbox

优先级：P2，触发式实施；依赖受控执行平面、Artifact 交付、运行幂等和结构化审计。设计边界见
[ADR-0007](adr/0007-deepagents-sandbox-runtime.md)。

- [ ] 定义平台无关的 `SandboxBackend` 与版本化 `SandboxProfile`，覆盖 create、upload、execute、
  download、destroy、scope、镜像、网络、文件系统、Secret 和资源限制；
- [ ] 将现有 Bubblewrap 收口为 `governed` Backend，并增加 OCI Backend 选项；校验实际运行时
  digest，不能只把声明值写入 provenance；
- [ ] 实现 `DeepAgentRuntimeAdapter`，接入统一 `RunRequest`、`AgentResult`、稳定 thread ID、deadline、
  usage、interrupt 和 Artifact 契约，不改变 LangGraph 默认路由；
- [ ] 增加 `bind_deepagent_capability`，让 Deep Agents Tool 继续经过同一个 `CapabilityExecutor`、
  Package Policy、审批、预算和审计；
- [ ] 实现 MMAG Skill 到 Deep Agents 的单向投影：默认只披露 instruction/reference/template，
  `resources.scripts` 保持隐藏，显式 `sandbox_editable` 资源除外；
- [ ] 自主 Sandbox 默认按 thread 隔离，模型/API/Mattermost/MCP Secret 留在控制面，不挂载项目源码、
  Artifact Repository 或其他 Run 工作区；
- [ ] 输入只允许经过 scope/kind/Hash 校验的 Artifact upload，输出必须 download、校验并原子提交为
  Artifact，禁止向用户返回 Sandbox 本地路径；
- [ ] 为 Sandbox 创建、命令执行、Artifact 提交和销毁增加稳定 execution key、幂等恢复、资源配额、
  取消、超时和异常回收；
- [ ] 仅向专用 Coding/Analysis/Media Package 开放 `sandbox.execute`，高风险命令按 Policy 进入
  LangGraph 审批；普通业务 Agent 和确定性 Presentation Agent 不获得通用 Shell；
- [ ] 至少选择一个可用于生产验收的 Deep Agents 兼容 Provider，并覆盖 Provider 不可用、网络隔离、
  越权文件、Secret 泄漏、超限输出、重复恢复和跨 thread 访问的负向测试；
- [ ] 建立与现有 LangGraph Runtime 的质量、成本、冷启动、执行时长和失败恢复对比，达到明确收益后
  才允许 Package 选择 `execution.kind=deepagent`。

退出标准：自主编程任务可在 thread-scoped Sandbox 内创建和运行临时代码，只能消费和产出受管
Artifact；LangGraph 仍是默认 Runtime，现有固定 Capability 不扩权，Sandbox 故障或 Provider 缺失
不会回退到宿主机执行。

## 当前明确不做

- 不恢复旧 Agent、Tool 或 Prompt 兼容入口；
- 不为了“多 Agent”数量重新注册没有 Package 契约的占位 Agent；
- 不用 Prompt 代替权限、审批、幂等和预算；
- 不向普通业务 Agent 暴露宿主机通用 Shell 或动态 Python；自主执行入口只有在“下一步 11”全部
  门禁完成后，才能向专用 Package 的独立 Sandbox 开放；
- 不允许 Agent/Skill Manifest 自授权或放宽平台执行配置；
- 不让 Agent Prompt 直接生成 Mattermost `props`、action callback 或平台专用协议；
- 不把富卡片/按钮作为唯一交互路径，不支持时必须降级为 Markdown/文本命令；
- 不在明文 HTTP 连接上传输 Bot Token 做管理级配置探测；
- 不在 Artifact/Scope/恢复语义未完成前拆微服务；
- 不把真实公网或 LLM 测试放进默认 CI。
