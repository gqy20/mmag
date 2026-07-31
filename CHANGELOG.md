# Changelog

## Unreleased

**工程能力**

- LangGraph 成为默认 Agent Runtime，接入官方 SQLite checkpointer、稳定 thread id 和原生 interrupt/resume
- 人工审批在 Capability 副作用前暂停，支持 approve/edit/reject、Mattermost 批准/拒绝命令及进程重启后恢复
- 移除无 checkpoint 的旧 LangGraph 循环与 `LegacyRuntimeAdapter`，Claude Agent SDK 改为显式 opt-in
- 清理重构残留：Capability binding 统一 LangGraph 命名，移除单子类 Adapter 层与未使用 API，合并 Runtime/执行器/模型输出过滤的重复实现
- 引入持久 Inbox/Outbox、按会话分区的并发调度和独立 Delivery worker；慢任务不再阻塞 WebSocket，投递重试不重跑 Agent
- 新增统一 LifecycleService、五类状态机、乐观版本、命令幂等、append-only 转换历史和重启 reconciliation
- 新增企业 Scope/Context、审批快照与恢复令牌、Artifact/Audit 模型，以及 Message/Profile/Knowledge/Summary/URL Repository 边界
- 新增 Managed Agent Registry/Router、Link Agent、Research/Project/Presentation Agent 和容错 handoff
- 新增 Policy Engine、Secret Provider、敏感数据脱敏、Model Gateway、actor 成本配额、metrics 和 SQLite 在线备份工具
- Mattermost 出站与附件进入带 timeout/retry 的共享异步连接池；trace context 改为任务级 ContextVar

- 默认测试集合隔离 PoC 与真实外部服务测试，新增离线消息主链契约
- 新增版本化 SQLite migration，支持旧库升级、失败回滚、历史校验和未来版本拒绝
- 将 schema 初始化、旧 `message_cache` 迁移与 CJK FTS 预处理从 `Memory` 下沉到 SQLite infrastructure
- Runtime 失败提示现在会投递给用户，不再被发送层静默丢弃
- 重整 AI Native 路线图，明确工程门禁、Runtime、Capability、执行解耦和企业 Context 的实施顺序与验收标准
- 收紧安全边界：Secret 日志仅报告是否配置、文件路径使用真实目录边界、未知 SDK/外部 MCP 工具默认拒绝
- `Memory.log_message` 的主表与 FTS 写入失败时完整回滚，避免后续提交半成品事务
- 新增统一 `make verify` 门禁：Ruff、255 个默认离线测试、分支覆盖率、mypy 与 wheel smoke
- 提交 `uv.lock` 并新增 GitHub Actions，CI 使用锁定依赖且不注入外部服务密钥
- `prompts.yml` 作为 wheel 包资源发布，同时支持 `PROMPTS_PATH` 显式覆盖
- 修复异步生成器工具被错误 `await`、二进制 WebSocket 消息和附件取消异常等类型检查发现的边界问题
- 重连重放的 Mattermost post 在执行前按持久化 ID 去重，避免重复模型调用与回复
- Mattermost 创建消息携带稳定 `pending_post_id`，连接错误、超时、429/5xx 最多重试 3 次，业务 4xx 不重试
- 新增不可变 `RunContext` / `RunRequest` / `AgentResult`、`AgentRuntime` Protocol 和统一 Runtime 错误模型
- Claude SDK 与 LangGraph 通过 Runtime 边界实现同一契约，统一 deadline、fallback 和错误翻译
- `Agent` 与 `MemoryCompactor` 已迁移到 Runtime Port，不再依赖后端私有异常和返回结构
- 新增不可变 `CapabilitySpec`、统一 `CapabilityExecutor`、策略元数据与稳定错误结果
- `get_channel_info` 已成为首个 Capability 垂直切片，由同一规格生成 LangGraph ToolRegistry 与 Claude SDK binding
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
