# Changelog

## Unreleased

**工程能力**

- 新增 Skill Package v1：严格 `skill.yml`、按需加载 `SKILL.md`、输入/输出 Schema、资源/eval Hash 与扁平版本目录
- 新增 Skill Registry/Resolver；消息主链先选 Agent 再选 Skill，并将模型工具、CapabilityContext 与 Policy 能力集合收窄到交集
- Agent Manifest 支持版本化 Skill allow/deny；Skill Required Capability 不能扩权，Skill/Instruction/Schema/eval provenance 写入成功运行审计
- 新增 `web-research@1.0.0`，由 `mmchat@1.1.0` 绑定；Skill scripts 在 v1 仅作为不可执行的哈希资源
- 新增 `report`、`ppt`、`project` 三个 LangGraph JSON Agent Package，以及 `report`、`slides`、
  `project` Skill；链接 Agent 升级为 `link@1.2.0` 并绑定 `link-read` 契约
- 三个新 Agent 分别使用独立默认拒绝 Policy：研报只读，PPT 文件交付和项目共享知识写入进入审批
- 结构化 Agent Prompt 可使用 parameters、context refs、artifact refs 和可信 conversation id
- Skill 资源实现三级渐进式披露：选中后只注入目录，模板/参考资料通过受 Policy 与预算约束的 `load_skill_resource` 按需读取
- 记录实际披露资源的 ref/Hash/字节/token provenance；LangGraph 审批暂停与恢复保留原资源会话
- 收紧企业执行闭环：过期、重复或 resume token 异常的审批不再恢复 LangGraph，非请求人必须具备 Mattermost 频道管理员或系统管理员身份
- Outbox Delivery 使用持久 delivery id 作为 Mattermost 幂等键，跨进程重试不再生成新 pending post id
- Inbox 新增持久 attempts/next_attempt_at 与瞬时错误有界重试；Task 终态延后到实际 Delivery 成功或失败
- 审批、Inbox 重试/失败和 Delivery 终态开始写入结构化 AuditEvent
- 新增 Agent Package v1：严格 Manifest JSON Schema、Prompt/Schema Registry、不可变版本发布与 Package/Prompt Hash
- 新增统一 Agent 输入/输出 Envelope 强制、Artifact Schema 校验和一次模型输出修复；二次失败返回稳定 `INVALID_OUTPUT`
- Policy Engine 改为默认拒绝，新增版本化 Policy Registry；Link Agent 接入独立 Package 与只读执行策略
- Agent、Policy、Model Policy 资源进入 wheel，成功结果携带完整版本 provenance
- Agent Package 改为扁平 `agents/<name>/agent.yml`；版本保留在 Manifest，Policy、Model Policy 和 eval Hash 合入发布快照
- 新增 YAML `execution/routing`、可信 Provider Registry 与 AgentFactory，Application 不再手工注册具体 Agent
- 默认 `mmchat` Prompt 迁入严格 Agent Package；删除全局 `prompts.yml`、`PromptManager` 和宽松缺变量行为
- 应用层拆为 composition root、消息编排、上下文/附件和 Delivery；删除 1617 行 `agent.py`
- Agent 领域收口到 `agent_system/`，Capability runtime binding 收口到 `capabilities/`；删除 `managed_agents.py` 和 `tools/` 兼容入口
- 默认消息主链强制经过 AgentRouter；移除没有 Package/Schema/Policy 的 Research、Project、Presentation 占位 Agent
- 新增严格 Model Policy Registry，并让 `mmchat/link` 都通过 Package 自动注册
- Capability 改为根据当前 Package 的 `policy_ref` 动态授权，删除全局 Bot Policy 绑定和专用 `LinkAgent`
- Claude SDK 工具可见性收紧为“已绑定 Capability ∩ 当前 Package allowlist”，不再暴露 CLI 内置文件/命令工具
- LangGraph 成为默认 Agent Runtime，接入官方 SQLite checkpointer、稳定 thread id 和原生 interrupt/resume
- 人工审批在 Capability 副作用前暂停，支持 approve/edit/reject、Mattermost 批准/拒绝命令及进程重启后恢复
- 移除无 checkpoint 的旧 LangGraph 循环与 `LegacyRuntimeAdapter`，Claude Agent SDK 改为显式 opt-in
- 清理重构残留：Capability binding 统一 LangGraph 命名，移除单子类 Adapter 层与未使用 API，合并 Runtime/执行器/模型输出过滤的重复实现
- 引入持久 Inbox/Outbox、按会话分区的并发调度和独立 Delivery worker；慢任务不再阻塞 WebSocket，投递重试不重跑 Agent
- 新增统一 LifecycleService、五类状态机、乐观版本、命令幂等、append-only 转换历史和重启 reconciliation
- 新增企业 Scope/Context、审批快照与恢复令牌、Artifact/Audit 模型，以及 Message/Profile/Knowledge/Summary/URL Repository 边界
- 新增 Managed Agent Registry/Router、Link Agent 和容错 handoff
- 新增 Policy Engine、Secret Provider、敏感数据脱敏、Model Gateway、actor 成本配额、metrics 和 SQLite 在线备份工具
- Mattermost 出站与附件进入带 timeout/retry 的共享异步连接池；trace context 改为任务级 ContextVar

- 删除过期 SDK PoC 脚本；真实外部服务测试继续通过 marker 与默认离线集合隔离
- 新增版本化 SQLite migration，支持旧库升级、失败回滚、历史校验和未来版本拒绝
- 将 schema 初始化、旧 `message_cache` 迁移与 CJK FTS 预处理从 `Memory` 下沉到 SQLite infrastructure
- Runtime 失败提示现在会投递给用户，不再被发送层静默丢弃
- 重整 AI Native 路线图，明确工程门禁、Runtime、Capability、执行解耦和企业 Context 的实施顺序与验收标准
- 收紧安全边界：Secret 日志仅报告是否配置、文件路径使用真实目录边界、未知 SDK/外部 MCP 工具默认拒绝
- `Memory.log_message` 的主表与 FTS 写入失败时完整回滚，避免后续提交半成品事务
- 新增统一 `make verify` 门禁：Ruff、默认离线测试、分支覆盖率、mypy 与 wheel smoke
- 提交 `uv.lock` 并新增 GitHub Actions，CI 使用锁定依赖且不注入外部服务密钥
- 修复异步生成器工具被错误 `await`、二进制 WebSocket 消息和附件取消异常等类型检查发现的边界问题
- 重连重放的 Mattermost post 在执行前按持久化 ID 去重，避免重复模型调用与回复
- Mattermost 创建消息携带稳定 `pending_post_id`，连接错误、超时、429/5xx 最多重试 3 次，业务 4xx 不重试
- 新增不可变 `RunContext` / `RunRequest` / `AgentResult`、`AgentRuntime` Protocol 和统一 Runtime 错误模型
- Claude SDK 与 LangGraph 通过 Runtime 边界实现同一契约，统一 deadline、fallback 和错误翻译
- 应用服务与 `MemoryCompactor` 已迁移到 Runtime Port，不再依赖后端私有异常和返回结构
- 新增不可变 `CapabilitySpec`、统一 `CapabilityExecutor`、策略元数据与稳定错误结果
- `get_channel_info` 已成为首个 Capability 垂直切片，由同一规格生成 LangGraph CapabilityRegistry 与 Claude SDK binding
- `search_knowledge` 已迁移到 Capability Catalog，统一默认条数、上限、结果格式与两端绑定
- `get_posts` 已迁移到 Capability Catalog，缓存优先、REST 回退与日志回填不再双重维护，并移出异步事件循环
- `search_messages` 已迁移到 Capability Catalog，统一过滤条件、毫秒转换和结果格式，并修复 SDK 忽略零时间戳的行为漂移
- `get_user_profile` 已迁移到 Capability Catalog，画像与用户名组合读取并发执行且两端共享排序和空结果语义
- `analyze_link` 已迁移到独立 Capability 模块，`SourcePolicy.AUTO` 由统一执行器注入可审计来源，删除 SDK 私有来源实现
- `save_knowledge` 已迁移为 `WRITE` Capability；统一 Authorizer 支持允许、拒绝和待审批，并在副作用前完成裁决
- `send_file` 已迁移为声明 `mattermost:file:write` 的 Capability；请求级不可变上下文替代全局 `ToolContext.current_post`
- Claude SDK 持久 MCP transport 通过查询串行化桥接请求上下文，避免并发请求的文件意图、频道和线程串线
- 外部 MCP discovery 统一转换为 `CapabilitySpec`，LangGraph/SDK 共享 schema、来源、授权和错误结果
- SDK 权限白名单由实际绑定能力动态生成，删除硬编码 `sdk_crawl_tools.py` 及 `crawl-mcp` 重依赖
- 八个内置能力改由单一有序 Catalog 生成，LangGraph/SDK 不再分别维护装配清单，`send_file` 两端可见

## 0.1.0 (2026-06-11) — 初始发布

**核心能力**

- WebSocket 实时连接 Mattermost(Mattermost 官方协议:握手认证/序列号校验/30s 心跳/指数退避重连/断线续传)
- Agentic Tool Use 循环(基于 `AsyncAnthropic`,LLM 可自主多轮调用工具)
- 三种触发方式:`@提及` / `DM 私聊` / 智能旁听(触发词+问句+概率三层判定)
- `prompts.yml` 单点配置 LLM 人格与触发规则,改词不改代码

**工具集(7 个内置)**

- `get_posts` — 获取频道消息历史(本地 SQLite 缓存优先,缓存不足回退 REST)
- `search_messages` — 按关键词/时间/用户/频道检索历史消息(FTS5 BM25 + CJK 预分词)
- `search_knowledge` — 搜索团队知识库
- `get_channel_info` — 查询频道详情
- `save_knowledge` — 写入团队知识库
- `get_user_profile` — 查看用户画像
- `analyze_link` — 链接分析(GitHub repo/PR/issue API + Trafilatura 全文 + SSRF 防护 + 1h 缓存)

**记忆系统**

- SQLite + FTS5 BM25 全文检索,支持中英文
- `message_log` 永久存储,启动时 backfill 补全 Mattermost 历史
- 长期记忆压缩器:按消息条数触发 LLM 摘要,以线程回复形式发到频道(原消息保留)
- 用户画像:活跃时段/话题词/沟通风格自动推断
- 团队知识库:LLM 主动沉淀决策/约定

**外部工具桥接**

- MCP(`.mcp.json` 配置),支持 stdio + SSE / Streamable-HTTP 两种传输

**工程能力**

- 链接分析底层(GitHub API + Trafilatura + SSRF 防护 + 缓存)以 `analyze_link` 工具暴露
- 分层 Logger + 启动时间戳分文件 + 自动清理 + 交互 trace_id 贯穿全链路
- 多环境配置(`.env` / `.env1` / `.env2` / `.env3`)
- `discover` CLI 工具(自动探测 Team/Channel/User ID)
- 兼容 Anthropic 官方接口与 StepFun 等兼容接口(`ANTHROPIC_BASE_URL` 切换)
