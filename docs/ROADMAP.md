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
- [x] `web-research@1.0.0` 作为首个可复用 Skill，由 `mmchat@1.1.0` 激活；
- [x] Skill 三级渐进式披露、Resource Hash 缓存、运行预算和审批 resume 恢复；
- [x] strict Prompt render、输入/输出/Artifact Schema 和预算强制；
- [x] Model Policy Registry 严格加载并校验 route。

### Mattermost 交付基线

- [x] 当前环境手动探测为 Mattermost Server `11.7.0` Entry，Database/Filestore 正常，`EnableFile=true`、`PluginsEnabled=true`、`AppsPluginEnabled=false`；
- [x] 已支持普通文本/Markdown Post、typing indicator、Thread 内 ack 和经审批的文件发送；
- [x] 文本 Outbox 具备幂等、重试、DLQ 和审计；
- [x] 已确认当前差距：普通最终回复未传 `root_id`，`OutboundMessage` 未承载 `file_ids`/Artifact/action，结构化 Agent 结果尚无 Mattermost 展示层；
- [x] 当前 `MM_URL` 为明文 HTTP，公开配置可读但认证配置探测不安全；未使用 Bot Token 执行探测。

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

## 下一步 3：执行 eval 发布门禁

优先级：P0。

- [ ] contract case 在激活前离线执行；
- [ ] quality case 定义数据集版本、阈值和评分器版本；
- [ ] eval 结果 Hash、发布时间和发布人进入发布记录；
- [ ] 失败 Package 不得生成发布制品或进入部署；
- [ ] 旧制品可按 Package Hash 原子回滚。

退出标准：坏 Prompt、坏 Schema、越权能力和质量回退不能进入新 Run。

## 下一步 4：Mattermost 响应展示与交互层

优先级：P1，作为 Research、Presentation、审批和 Artifact 交付的公共基础。

- [ ] 定义平台无关的 `ResponseView` 契约，统一表达 `kind`、`title`、`summary`、`sections`、`sources`、`warnings`、`artifacts`、`actions` 和 Run 状态；
- [ ] 实现 `ResponsePresenter`，将 `AgentOutput.structured_result/artifacts/envelope` 转为 `ResponseView`，禁止将结构化 JSON 直接展示给默认用户；
- [ ] 实现 `MattermostRenderer`，将 `ResponseView` 确定性地映射为 Markdown、`props.attachments`、文件附件和 action，对模型/外部内容做长度限制、转义与安全链接处理；
- [ ] 扩展 `OutboundMessage` 和 SQLite Outbox，持久化 `root_id`、`message_kind`、`artifact_refs/file_ids`、`props`、`actions`、`update_post_id` 及稳定幂等键；
- [ ] 统一 Thread 策略：ack、进度、审批、附件、错误和最终结果均继承原始 `root_id`，避免在频道中散落；
- [ ] 将 `get` ack 替换为可配置的简短确认；短任务仅使用 typing，长任务创建单个状态 Post 并原地更新，阶段必须来自真实 Lifecycle Event，不伪造进度百分比或 ETA；
- [ ] 实现 Markdown-aware 长消息分段，保留标题、链接、代码围栏和序号；详细报告默认“Thread 摘要 + Artifact 附件”；
- [ ] 将 `send_file` 的直接上传/发帖副作用收口到 Delivery/Outbox：执行层只产出 Artifact ref，交付层在审批后上传、绑定 `file_ids` 并重试；
- [ ] 为 Link/Research 结果提供稳定 Presenter，在 Thread 中展示结论、关键字段、来源和警告，完整 JSON 仅作为可下载 Artifact 或审计数据；
- [ ] 实现签名、短时、一次性 Action Token 和 Callback Endpoint，支持批准/拒绝/重试/下载/返工，每次回调重新校验 actor、scope、资源和当前状态；
- [ ] 交互按钮优先使用 Mattermost `props.attachments`/action，不支持或回调不可达时降级为 `批准 <id>` / `拒绝 <id>` 文本命令；
- [ ] 实现统一错误展示分类：输入问题、权限不足、等待审批、资源耗尽、执行超时、外部依赖失败和系统故障；对用户隐藏秘密/堆栈，保留可查询 Run ID；
- [ ] 增加可重复的 Mattermost 能力探测，记录 Server 版本、Edition、文件/插件/交互开关和客户端兼容矩阵；认证级探测必须经 HTTPS 或本机受信通道；
- [ ] 覆盖 Web/Desktop/Mobile 的 Markdown、Thread、分段、重试、回调幂等、按钮降级和附件失败测试。

退出标准：任何 Agent 都只产出平台无关结果；Mattermost 用户在一个 Thread 中获得可读摘要、真实进度、来源、可下载 Artifact 和可审计操作；富交互不可用时仍能用 Markdown/文本命令完成同一流程。

## 下一步 5：Research Package

优先级：P1。

- [x] 定义 `report` Manifest、只读 Policy、Prompt 和输入/输出 Schema；
- [x] 新增 `report@1.0.0` Skill，将结果升级为证据账本式结构化输出；
- [x] 定义 `research_report` Artifact Schema；
- [ ] 接入来源去重、时效性和证据覆盖 eval；
- [ ] 将报告持久化到 Artifact Repository；
- [ ] 覆盖取消、超时、部分来源失败和预算耗尽。

退出标准：Research 只能读允许来源，输出始终是可验证、有来源、有版本的 Artifact。

## 下一步 6：受控 Python/CLI 执行平面

优先级：P1，为 Presentation 等生成型 Agent 提供基础能力。

- [ ] 定义版本化 `ExecutionProfile` YAML、Schema 与 Registry，将 runner、镜像摘要、固定可执行文件、环境变量、文件系统、网络和资源限制纳入 Hash/provenance；
- [ ] 实现通用 `ProcessRunner` 和受管 `ScriptExecutor`，只使用固定 argv 启动已注册的可执行文件，禁止 `shell=True`、任意命令字符串和动态 `eval/exec`；
- [ ] 执行前校验 Skill `resources.scripts` 声明、Package Hash 和脚本 SHA-256；Skill 脚本不可作为普通可披露资源；
- [ ] 每个 Run 创建独立临时工作区，只读挂载已验证的 Skill 脚本/模板，仅允许写入 Run tmp 和 Artifact staging；
- [ ] 强制超时、CPU、内存、进程数、输出大小和默认断网；秘密不继承，只能通过显式授权注入；
- [ ] 将 Agent `capabilities.allow/deny`、Skill `required/optional_capabilities`、Package Policy 与 Execution Profile 计算为最终权限交集；Skill 和 Agent Manifest 只能缩小权限，不得自授权或放宽执行配置；
- [ ] 优先注册 `ppt.render`、`ppt.export_pdf` 等窄口 Capability，不向模型暴露通用 `shell.exec` 或 `python.eval`；
- [ ] 将命令摘要、执行配置版本、脚本 Hash、返回码、资源用量、超时/错误码和产物 Hash 写入结构化审计事件，日志不记录秘密和完整敏感输入；
- [ ] 成功输出原子提交到 Artifact Repository，失败/取消/超时清理 staging，临时目录按 retention 统一回收；
- [ ] 覆盖命令注入、路径穿越、符号链接逃逸、篡改脚本、超时、资源耗尽、未授权网络和越权 Artifact 等负向测试。

退出标准：Agent 只能调用 Agent、Skill、Policy 和 Execution Profile 共同允许的固定执行能力；任意命令、未声明脚本、越界文件访问和未授权网络请求均在副作用前被拒绝，临时文件、Artifact 和审计记录可按 Run 追踪。

## 下一步 7：Presentation Package 与严格 handoff

优先级：P1，依赖下一步 4、5、6。

- [ ] Presentation 只接受 `research-report` ref；
- [ ] 输入 Artifact 在执行前校验版本和 scope；
- [x] 定义 `slides@1.0.0` Skill、演示叙事模板和严格输出契约；
- [x] 定义 `ppt` Agent Manifest 和默认拒绝 Policy，对外交付要求审批；
- [ ] 在 Skill 中接入受管脚本以及 `ppt.render` / `ppt.export_pdf` 能力；
- [ ] 通过受控执行平面输出 presentation outline/file/preview Artifact；
- [ ] 交付同时生成 PPTX、可预览 PDF 和封面/关键页 PNG，由 Mattermost Presenter 组装摘要、预览与下载入口；
- [ ] 文件交付仍经过审批和 Outbox；
- [ ] handoff 每一步持久化状态、失败、重试和成本。

退出标准：Research → Presentation 不传自由文本；非法或越权 Artifact 不能进入下游；PPT 生成不依赖宿主机通用 Shell/Python 权限。

## 下一步 8：反馈、返工与业务闭环

优先级：P1。

- [ ] 用户可接受、驳回或请求返工；
- [ ] 返工创建新 Run，关联原 Artifact，不覆盖历史；
- [ ] 反馈进入质量数据集但默认不进入 Prompt；
- [ ] Task 只有在交付被接受或明确终止后才闭环；
- [ ] 建立任务、AgentRun、Artifact、Delivery、Feedback 的端到端报表。

退出标准：企业任务从请求到交付、审批、验收、返工和审计形成可查询闭环。

## 下一步 9：结构化可观测性与部署验收

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
- 不向模型暴露通用 Shell 或动态 Python 执行入口；
- 不允许 Agent/Skill Manifest 自授权或放宽平台执行配置；
- 不让 Agent Prompt 直接生成 Mattermost `props`、action callback 或平台专用协议；
- 不把富卡片/按钮作为唯一交互路径，不支持时必须降级为 Markdown/文本命令；
- 不在明文 HTTP 连接上传输 Bot Token 做管理级配置探测；
- 不在 Artifact/Scope/恢复语义未完成前拆微服务；
- 不把真实公网或 LLM 测试放进默认 CI。
