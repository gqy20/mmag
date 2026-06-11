# mmag — Mattermost AI Agent

通过 WebSocket 实时连接 Mattermost，以 Bot 身份参与团队对话。

## 架构

```
WebSocket (收消息) → 触发引擎 → LLM 推理 → REST API (发消息) → 记忆存储
```

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
BOT_NAME=小智                  # Bot 名称
LISTEN_PROBABILITY=0.15        # 主动旁听概率 (0~1)
MAX_CONTEXT_MESSAGES=100       # 上下文窗口消息数 (传给 LLM 的最近 N 条)
MAX_CONTEXT_CHARS=10000        # 上下文窗口总字符上限 (按 token 粗估)
MEMORY_SUMMARY_INTERVAL=100    # 每 N 条消息触发一次定期摘要
MEMORY_CONTEXT_WINDOW=100      # 摘要时注入的前序上下文消息数
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
| `make clean` | 清理缓存和编译文件 |

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

1. **@提及** — 在消息中 `@小智`，必回复
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
│   ├── memory.py               # SQLite 持久化记忆 (Layer 1+2)
│   ├── memory_compactor.py     # 长期记忆压缩器
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
    └── MATTERMOST_ID_GUIDE.md  # Mattermost ID 层级参考
```

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
- **LLM 适配**：`AsyncAnthropic` 原生异步客户端（SDK 内置 `max_retries=2`）；Agentic Tool Use 循环 + ThinkingBlock 自动过滤
- **LLM 兼容**：通过 `ANTHROPIC_BASE_URL` 支持 StepFun 等兼容接口；调用失败抛 `LLMError` 由 agent 层转成用户友好提示

## License

MIT
