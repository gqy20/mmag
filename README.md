# mmag — Mattermost AI Agent

通过 WebSocket 实时连接 Mattermost，以 Bot 身份参与团队对话。

## 架构

核心数据流：

```text
Mattermost WebSocket → InboundEvent → Inbox → conversation scheduler
                                              ↓
                                      Managed Agent / Runtime
                                              ↓
                              Capability + Policy + Lifecycle
                                              ↓
                                           Outbox
                                              ↓
                                Delivery → Mattermost REST
```

关键点:
- WebSocket 回调只负责持久接收；同会话串行、跨会话并发
- Agent 执行与 Delivery 状态独立，失败投递不会重复调用模型
- Lifecycle、审批、审计、配额和企业 Context 共用 SQLite 控制面
- LLM 循环在 `llm.py:agent_loop`,最多 `MAX_TOOL_ROUNDS` 轮,达到上限返回 "处理超时"
- 工具系统统一在 `ToolRegistry`,内置工具 + MCP 注入工具走同一调度
- 记忆读写双向:消息实时写入 + 启动 backfill 补全,LLM 检索走 FTS5 BM25

## 快速开始

### 1. 环境要求

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/)（包管理器）
- 一个 Mattermost 服务器 + Bot Account Token
- LLM API Key（支持 Anthropic Claude 或 StepFun 兼容接口）

### 2. 配置

复制或创建 `.env` 文件：

```bash
# Mattermost
MM_URL=https://your-mattermost-server
MM_TOKEN=your-bot-token
MM_TEAM_ID=                    # Team ID（可选，留空监听所有）
MM_CHANNEL_ID=                 # Channel ID（可选，留空监听 Team 下所有频道）

# LLM（二选一）
ANTHROPIC_API_KEY=sk-xxx       # Anthropic / StepFun API Key
ANTHROPIC_BASE_URL=            # 留空用官方 API，或填 StepFun 兼容地址
ANTHROPIC_MODEL=step-3.7-flash # 模型名称

# Agent
# 触发策略: @/DM/thread 走硬规则,其他 LLM 自主决策(<SILENT> 标记沉默)
MAX_CONTEXT_MESSAGES=100       # 上下文窗口消息数 (传给 LLM 的最近 N 条)
MAX_CONTEXT_CHARS=10000        # 上下文窗口总字符上限 (按 token 粗估)
MEMORY_SUMMARY_INTERVAL=100    # 每 N 条消息触发一次定期摘要
MEMORY_CONTEXT_WINDOW=100      # 摘要时注入的前序上下文消息数
PIPELINE_MAX_CONCURRENCY=8     # 跨会话并发
PIPELINE_MAX_PENDING=256       # 入口背压上限
RUNTIME_DEADLINE_SECONDS=120   # 单次 Agent Run deadline
MODEL_BUDGET_USD=100           # 每 actor 的进程内成本上限
```

> **不知道 Team/Channel ID？** 先运行 `make discover` 自动探测。

### 3. 安装 & 运行

```bash
# 安装依赖
make install

# 探测环境 ID（首次配置推荐）
make discover

# 启动 Agent
make run
```

## 使用指南

### 命令

| 命令 | 说明 |
|------|------|
| `make run` | 启动 Agent，连接 Mattermost 并开始监听 |
| `make discover` | 探测服务器上的 Team / Channel / User ID |
| `make install` | 以 editable 模式安装包 |
| `make test` | 运行默认离线测试集 |
| `make verify` | 执行与 CI 一致的 Ruff、coverage、mypy 和 wheel smoke 门禁 |
| `make clean` | 清理缓存和编译文件 |

### 开发与验证

```bash
# 按锁文件安装开发依赖
uv sync --locked --dev

# 提交前执行完整工程门禁
make verify
```

默认测试不连接真实 Mattermost、LLM 或公网；这类验证标记为 `external`，需要显式执行。当前 coverage 分支覆盖率基线为 40%，类型检查覆盖 `src/mmag`。构建出的 wheel 会在临时目录解包，验证包、CLI 模块和内置 `prompts.yml` 均可脱离源码树加载。

### Discover 高级用法

```bash
# 指定环境文件
make discover -- --env .env2

# 只看某个 Team
make discover -- --team my-team

# 探测后自动写入 .env
make discover -- --update-env

# JSON 输出（方便脚本处理）
make discover -- --json
```

### 如何与 Bot 对话

Bot 支持三种触发方式：

1. **@提及** — 在消息中 `@<bot_username>`(由 MM_TOKEN 决定,如 `agent2`),必回复
2. **DM 私聊** — 直接私聊 Bot，必回复
3. **智能旁听** — 群聊中自动检测问题、帮忙请求等，按概率响应（默认 15%）

无需斜杠命令，纯自然语言驱动。

## 项目结构

```
├── Makefile                    # 快捷命令
├── pyproject.toml              # 项目配置
├── .env                        # 环境配置
├── prompts.yml                 # 系统提示词模板
├── src/mmag/
│   ├── __init__.py             # 包入口 (暴露 Agent/Config)
│   ├── cli.py                  # CLI 入口
│   ├── config.py               # 配置加载 (.env)
│   ├── prompts.py              # 提示词管理
│   ├── logger.py               # 日志 (控制台 + 按日分文件 + 自动清理)
│   ├── memory.py               # 记忆业务接口 (Layer 1+2)
│   ├── memory_compactor.py     # 长期记忆压缩器
│   ├── infrastructure/
│   │   └── sqlite/             # SQLite 连接、版本化迁移与 FTS 预处理
│   ├── runtimes/               # Provider-neutral Runtime 契约与 SDK/Legacy Adapter
│   ├── capabilities/           # 单一能力规格、统一执行器与 Runtime bindings
│   ├── control_plane/          # Inbox/Outbox、Lifecycle、Context、Approval
│   ├── governance/             # Policy、Secret、Model Gateway、Quota、运维原语
│   ├── managed_agents.py       # Agent Registry、Router、handoff 与 Link Agent
│   ├── repositories.py         # 专用 Memory Repository 边界
│   ├── llm.py                  # LLM 适配器 (AsyncAnthropic + Agentic Tool Use)
│   ├── client.py               # Mattermost REST API 客户端 (元数据缓存)
│   ├── url_analyzer.py         # 链接分析 (GitHub / Trafilatura / SSRF 防护)
│   ├── mcp_bridge.py           # MCP 外部工具桥接 (.mcp.json)
│   ├── ws_client.py            # Mattermost WebSocket 协议实现
│   ├── agent.py                # 核心 Agent (编排消息处理 + 工具调用)
│   ├── discover.py             # 环境 ID 探测工具
│   └── tools/                  # 工具注册 + 内置工具集
│       ├── registry.py
│       └── builtin.py
└── docs/
    ├── adr/                     # 已接受的架构决策记录
    ├── AI_NATIVE_REFACTORING.md # AI Native 目标架构与设计理由
    ├── ROADMAP.md               # 当前状态、后续步骤与验收标准
    ├── TECH_DEBT.md             # 已知技术债清单
    ├── OPERATIONS.md            # 私有化部署、备份恢复与告警基线
    └── MATTERMOST_ID_GUIDE.md   # Mattermost ID 层级参考
```

## 文档导航

- [AI Native 重构方案](docs/AI_NATIVE_REFACTORING.md)：目标架构、设计原则与阶段依赖；
- [实施路线图](docs/ROADMAP.md)：当前完成情况、下一步任务和各阶段退出标准；
- [技术债清单](docs/TECH_DEBT.md)：具体问题、风险和建议拆分方向；
- [Runtime 选择 ADR](docs/adr/0001-runtime-selection.md)：默认 Runtime、失败边界和 Legacy 退出条件；
- [Capability 授权 ADR](docs/adr/0002-capability-authorization.md)：写能力三态裁决、默认策略和审批扩展边界；
- [请求级 Capability Context ADR](docs/adr/0003-request-scoped-capability-context.md)：异步上下文隔离与持久 SDK 桥接策略；
- [MCP Capability Adapter ADR](docs/adr/0004-mcp-capability-adapter.md)：外部工具发现、双 Runtime binding 与统一 Policy；
- [Mattermost ID 指南](docs/MATTERMOST_ID_GUIDE.md)：Team、Channel 和 User ID 的获取与配置。

## 记忆系统

Bot 具备跨会话持久记忆：

| 类型 | 存储内容 | 说明 |
|------|----------|------|
| **消息日志** | 原始消息 + FTS5 索引 | 永久，启动时 backfill 补全历史，供 LLM 检索/回顾 |
| **用户画像** | 专业领域、偏好风格 | 跟踪每个用户的特征 |
| **团队知识** | 关键事实 + 置信度 | 从对话中提取并积累 |
| **对话摘要** | 话题摘要 + 要点 | 长期，定期压缩（不删原消息） |

数据存储在本地 SQLite (`agent_memory.db`)。

## 多环境支持

项目可维护多份 `.env` 配置：

| 文件 | 用途 |
|------|------|
| `.env` | 主环境（默认加载） |
| `.env1` | 备选服务器 |
| `.env2` | 同服务器不同频道 |
| `.env3` | 本地 Docker 开发 |

切换环境：修改 `discover.py` 中的 `--env` 参数指向对应文件。

## 技术细节

- **WebSocket 协议**：完整实现 Mattermost 官方协议（握手认证、序列号校验、30s 心跳、指数退避重连）
- **断线续传**：通过 `connection_id` + `sequence_number` 实现断线后恢复
- **消息永久存储**：`message_log` 表只增不删，启动时 backfill 补全 Mattermost 端所有历史；FTS5 虚表（unicode61）支持中英文 BM25 全文检索
- **Schema 演进**：启动时按版本顺序执行原子 migration；支持旧库字段补齐、`message_cache` 数据/FTS 迁移、失败回滚及未来版本拒绝
- **MCP 权限**：外部 MCP 默认不连接，需通过 `MCP_ALLOWED_TOOLS` 精确授权 `mcp_<server>_<tool>`；命中后统一进入 Capability Policy，并在 SDK/Legacy 中保持相同可见性
- **消息可靠性**：重复 `posted` 事件在 Runtime 调用前按持久化 post ID 去重；创建回复使用 `pending_post_id` 对瞬时故障做有界幂等重试
- **Runtime 边界**：应用层统一使用不可变 `RunRequest` / `AgentResult` 和 `AgentRuntime`，SDK/Legacy 的异常与 fallback 由 Adapter 收口
- **Prompt 资源**：默认使用 wheel 内置 `prompts.yml`；开发时可设置 `PROMPTS_PATH` 显式覆盖
- **工程门禁**：`make verify` 是本地与 CI 的统一入口，依赖由提交到仓库的 `uv.lock` 固定
- **长期运行注意**：message_log 持续累积，生产环境建议定期 `VACUUM INTO` 归档老消息（参考月度一次），避免 SQLite 库文件膨胀影响性能（数据保留周期按团队合规要求自行决定）
- **LLM 适配**：`AsyncAnthropic` 原生异步客户端（SDK 内置 `max_retries=2`）；Agentic Tool Use 循环 + ThinkingBlock 自动过滤
- **LLM 兼容**：通过 `ANTHROPIC_BASE_URL` 支持 StepFun 等兼容接口；调用失败抛 `LLMError` 由 agent 层转成用户友好提示

## License

MIT
