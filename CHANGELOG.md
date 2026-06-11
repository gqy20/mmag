# Changelog

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
