# 技术债清单 (Tech Debt)

本文件汇总当前未解决的技术债,按优先级分级,供后续排期参考。

---

## P1 — 生产稳定性 (建议本月内修复)

> 触发条件: 网络抖动 / Token 过期 / LLM 限流时可能导致消息丢失、Agent 失能、用户体验下降

### 1. MMClient REST 调用无重试
- 位置: `src/mmag/client.py:47-55, 98-105, 113-116, 127-136`
- 问题: `_get` / `_post` / `send_post` / `send_ephemeral` / `get_posts_page` 全部无重试;`raise_for_status()` 失败直接抛 `HTTPError`,Mattermost 瞬时 502/网络抖动会丢消息
- 方案参考: `tenacity` 库指数退避 / 手写 retry decorator

### 2. WebSocket 重连无次数上限,401 无告警
- 位置: `src/mmag/ws_client.py:43-48, 90-111`
- 问题: 重连参数 `_MIN_RETRY_S=3` / `_MAX_RETRY_S=300`,但**没有重试次数上限**;Token 过期后 401 永久重连,Agent 看似在线实则完全无功能,运维侧无告警
- 额外: 前 7 次重连间隔固定 3s,可能对服务端形成"重连风暴"
- 方案参考: 加 401 专项告警 + 重连次数熔断 + Token 有效性探测

### 3. LLM 异常类型不细分
- 位置: `src/mmag/llm.py:50-74, 88-153`
- 问题: `APITimeoutError` / `RateLimitError` / `BadRequestError` / `PermissionDeniedError` / 内容审核拦截 — **全部**包装为 `LLMError`,调用方无法区分
- 调用方 (`src/mmag/agent.py:471-474`) 一律返回 "LLM 暂时不可用",**对审核拦截给这个提示是误导**(再久也复现)
- 方案参考: 拆 `LLMError` 为 `LLMTimeout` / `LLMRateLimit` / `LLMRejected` / `LLMUnavailable`,agent 层按类型做不同 fallback

### 4. LLM 失败无指标、无降级
- 位置: `src/mmag/agent.py:471-474`
- 问题: LLM 失败时返回固定字符串,无重试、无降级到本地规则、无 metric
- 连续 10 次失败用户看到 10 条相同提示,运维侧无法从指标识别问题
- 方案参考: 加 `stats["llm_failures"]` 计数 + 指数退避重试 + 显式 ping 用户告知"正在恢复"

### 5. 关键路径 e2e 测试缺失
- 位置: `tests/` (仅 1 个文件 `test_url_analyzer.py`)
- 缺失覆盖的关键链路:
  - WebSocket 收消息 → 触发引擎 → LLM → 工具调用 → 回复 (整链 0 覆盖)
  - LLM `agent_loop` 多轮工具调用 (`llm.py:88-251`)
  - 消息路由触发逻辑 (`agent.py:373-401`)
  - `_build_context` 双阈值裁剪 (`agent.py:550-567`)
  - Backfill 分页拉取 (`agent.py:223-263`)
  - MCP stdio / SSE 两种传输 (`mcp_bridge.py:179-256`)
  - 错误路径(`LLMError` / `send_post` 失败 / `log_message` 失败等)
- 方案参考: 引入 `pytest-asyncio` + `httpx_mock` + 真实 ws mock 框架,优先补 e2e 再补单元

---

## P2 — 架构债 (建议下季度修复)

> 触发条件: 单文件超过 500 行 / 职责超过 4 个 / 修改一处需联动多处的设计

### 6. agent.py — Facade God Object (7 个职责)
- 位置: `src/mmag/agent.py` (629 行)
- 职责:
  1. 编排启动流程 (`start`, line 68-188)
  2. WebSocket 事件分发 (`_on_ws_event` / `_on_posted`, line 190-278)
  3. Backfill 历史 (`_backfill_channel`, line 223-263)
  4. 触发判定 (`_should_respond`, line 403-443)
  5. LLM 上下文构造 (`_build_context`, line 491-569)
  6. Typing 模拟 (`typing_indicator`, line 571-577)
  7. 消息回写 + 生命周期 (`reply` / `stop`, line 579-628)
- 特别严重: `_on_posted` 单函数 125 行,5 个提前 return
- 方案参考: 拆 `MessageRouter` / `ContextBuilder` / `TriggerJudge` / `ResponseSender`

### 7. memory.py — 多 Repository 强耦合 (7 个职责)
- 位置: `src/mmag/memory.py` (1012 行)
- 职责:
  1. Schema 迁移 (`_init_db` / `_migrate_message_cache_to_log`)
  2. 消息日志 (`log_message` / `get_recent_messages` / `search_messages` / FTS5)
  3. URL 缓存 (`cache_url` / `get_cached_url`)
  4. 用户画像 (`update_profile_from_message` / `_extract_topic_keywords` / `_infer_style` / `_initial_style`)
  5. 团队知识 (`add_knowledge` / `get_relevant_knowledge`)
  6. 摘要 (`save_conversation_segment` / `get_recent_summary`)
  7. CJK 预分词 (`_cjk_tokenize_for_fts`)
- 方案参考: 拆为 `repositories/message_log.py` / `url_cache.py` / `user_profile.py` / `team_knowledge.py` / `summary.py`

### 8. url_analyzer.py — 混合 transport / business / cache
- 位置: `src/mmag/url_analyzer.py` (725 行)
- 职责: SSRF 防护 / HTML OG 解析 / 正文提取 / GitHub API 客户端 / 通用网页抓取 / 缓存控制
- 方案参考: 拆为 `ssrf_guard.py` / `github_api.py` / `html_extractor.py` / `cache.py`

### 9. async/await 风格混用
- 同步 `requests` 阻塞异步流程
  - `src/mmag/client.py:38-55` 整个 `MMClient` 用 `requests`,但调用方都在 `async def`
  - `src/mmag/memory.py` 同步 SQLite,但 `agent.py:345, 255, 599` 等都在 `async` 流程里直接调用
- 同步 `time.sleep` 在 async 上下文
  - `src/mmag/agent.py:223 _backfill_channel` 是 `def` 而非 `async def`,用 `time.sleep(0.1)`
- 同步/异步 handler 动态判断
  - `src/mmag/tools/registry.py:113-117` 用 `inspect.iscoroutine()` 动态判断是否 await,容易让同步 handler 静默阻塞
- 双重计数
  - `src/mmag/llm.py:56, 128` `call_count` 在 `chat` 和 `agent_loop` 各自自增,无 tools 时 fallthrough 双重计数
- 方案参考: `MMClient` 改 `httpx.AsyncClient`;`_backfill_channel` 改 `async def` + `await asyncio.sleep`;统一要求 handler 是 async

### 10. 依赖与配置清理
- 位置: `pyproject.toml:8-17`
- 问题:
  - `mcp[cli]` 引入 click/typer 等本项目用不到的 CLI 工具 → 改 `mcp`
  - `requests` 与 `httpx` **并存**:`client.py` 用 `requests`,`url_analyzer.py` 用 `httpx`
  - 所有依赖用 `>=` 而非 `~=`,**无版本上限**
  - 无 `mypy` / `pyright` / `coverage` 等静态检查工具
- 方案参考: 删 `mcp[cli]` extra / 统一 `httpx` / 加版本上限 / 引入 mypy

### 11. __init__.py 副作用过重
- 位置: `src/mmag/__init__.py:7-8`
- 问题: `from .agent import Agent` 会拉起 LLM SDK + WebSocket + MCP + Memory + URL Analyzer 整条链路
- 副作用: `from mmag import Config` 会触发 Agent 全部依赖,纯配置/类型检查场景被污染
- 方案参考: `__init__.py` 只放 `__version__`,`Agent` / `Config` 走显式 `from mmag.agent import Agent`

### 12. 配置无 schema 校验
- 位置: `src/mmag/config.py:67-105`
- 问题: `listen_probability=float(os.getenv("LISTEN_PROBABILITY", "0.15"))` 若环境变量是 "abc" 会 crash;无范围校验
- 方案参考: 改 pydantic BaseSettings / `dataclass` + 自定义 `__post_init__`

---

## P3 — 工程细节债 (可后续清理)

> 触发条件: 风格不统一 / 命名不清晰 / 异常处理不一致 / 重复代码

### 13. 错误处理风格不统一
- 30+ 处 `except Exception`,**无项目级规范**
- `log.debug` vs `log.error` 同一类失败在不同模块用不同级别(如 `memory.py:282` 用 debug,`agent.py:613` 用 error)
- 错误信息缺少上下文(`log.error("发送消息失败: {e}")` 无 `channel_id` / `post_id`)
- 错误返回结构不统一:`url_analyzer._err_dict` 返回结构化 dict,`memory` / `client` 返 None,`mcp_bridge` 返 JSON

### 14. 命名风格混杂
- `agent.py:190-617`: `_on_ws_event` / `_on_posted` / `_should_respond` / `_respond` / `_build_context` / `typing_indicator` / `reply` / `_backfill_channel` 风格混杂
- `agent.py:51 working_memory` 同时是"消息流"和"上下文窗口",语义模糊
- `agent.py:445 _respond` 的 `tag` 参数含义模糊(注释说"mention / chat"但 chat 同时用于 DM 与旁听)

### 15. 命名 `get_descriptions` 已被删,仍有其他可疑命名
- `tag` 参数语义压扁 (见 #14)
- `working_memory` 命名不清 (见 #14)

### 16. 函数过长
| 文件:行号 | 函数 | 行数 |
|---|---|---|
| `agent.py:68` | `start` | 121 |
| `agent.py:278` | `_on_posted` | 125 |
| `url_analyzer.py:388` | `_process_github_repo_response` | 86 |
| `llm.py:88` | `agent_loop` | 86 |
| `agent.py:491` | `_build_context` | 80 |
| `tools/builtin.py:394` | `_format_link_info` | 78 |
| `url_analyzer.py:545` | `_analyze_webpage` | 76 |
| `url_analyzer.py:642` | `analyze_url` | 74 |
| `memory_compactor.py:157` | `_summarize_message_batch` | 73 |
| `url_analyzer.py:471` | `_analyze_github_issue_impl` | 67 |
| `memory_compactor.py:87` | `_periodic_summary` | 66 |
| `tools/registry.py:92` | `execute` | 53 |
| `ws_client.py:139` | `_session` | 51 |

### 17. 深层嵌套
| 文件:行号 | 嵌套层级 |
|---|---|
| `agent.py:241-261` | 5 |
| `url_analyzer.py:167-204` `_is_safe_url` | 5 |
| `agent.py:516-528` | 4 |
| `agent.py:552-561` | 4 |
| `llm.py:237-250` | 4 |

### 18. 类型注解缺失
- `agent.py:29/68/619` 等多处 `__init__` / `start` / `stop` 缺 `-> None`
- `agent.py:265/278/445/491` 等多处方法签名用 `dict` 太宽(应 `TypedDict` 或 Pydantic)
- `tools/builtin.py:39-478` 7 个工厂函数参数全部无类型
- `llm.py:267 _parse_response(response: Any)` 应 `Message`
- `ws_client.py:222 _ping_loop(self, ws)` `ws` 缺类型
- `url_analyzer.py:642 analyze_url(memory=None)` `memory` 缺类型

### 19. 重复代码
- `memory.log_message` 调用方模式重复 (`agent.py:255/345/599`, `builtin.py:512` 都做相同字段补充)
- LLM 调用 kwargs 拼装重复 (`llm.py:50-74` vs `llm.py:88-153` 两份几乎一致的 create 调用)
- `summary_parts` 构造瀑布式 if 链重复 (`url_analyzer.py:407-425` 与 `512-522` 结构高度相似)
- `_respond → agent_loop` 与 `compactor → chat_with_system` 流水线重复
- `_make_*_tool` 7 个工厂同构 (虽然已提常量,但仍有 description vs lambda 默认值两套机制)

### 20. 魔数 / 硬编码常量
- `agent.py:240 per_page=200` / `:261 time.sleep(0.1)` / `:382 max_rounds=5` 等魔数
- `agent.py:374-378` 25 个中文触发词 (已抽到 `prompts.yml/triggers/`,但代码内仍有几处变体)
- `url_analyzer.py:432, 434, 249, 570` 200KB / 500KB / 5000 字截断长度字面
- `agent.py:236, 606` 毫秒转换 `int(time.time() * 1000)` 重复
- `memory_compactor.py:195 ts < 1e12` 毫秒/秒分界魔法常数

### 21. 重复函数体内 import
- `agent.py` 顶部已 import,函数内 `import time` (line 232)
- `memory.py:488, 559-560, 595, 677, 1000` 多处函数内 import

### 22. SQLite 并发与事务保护
- `src/mmag/memory.py:23` `check_same_thread=False` + **全代码无锁**
- 跨线程并发可能 corrupt (虽然 Agent 当前是单线程,加并发 worker 后会触发)
- 多步操作无 try/except 包裹事务回滚 (P0 已修 `log_message` 部分,但 `add_knowledge` 仍有主表/FTS 不一致风险)

### 23. 摘要计数不清零 / 无重试退避
- `src/mmag/memory_compactor.py:71-81` `maybe_compact` 用 `_msg_counter` 字典,失败时不清零,LLM 限流时可能连续触发
- 摘要 LLM 失败无重试 (P0-2 已修"失败不入库",但 LLM 瞬时失败仍会跳过整个批次)

### 24. WebSocket 内部细节
- `ws_client.py:222 _ping_loop` 只捕 `CancelledError` + `ConnectionClosed`,其他异常(`RuntimeError`)冒泡导致 ping task 死掉无日志
- `ws_client.py:184-188` `_session` 异常时 `self._ws = None` 清理依赖 `_cleanup_ping_task` finally,异常路径下未确认一定执行
- `ws_client.py:212-215` 序列号不连续只 `log.warning`,**没统计丢包次数**

### 25. 全局 httpx 客户端生命周期
- `src/mmag/url_analyzer.py:101-124` `_client` 单例无线程/事件循环保护
- `close_client` 已通过 P0-5 加入 `agent.stop`,但**单元测试场景**仍依赖此单例,测试间可能状态污染

### 26. TODO 2 处未排期
- `src/mmag/agent.py:213 post_edited` → `_noop` (TODO)
- `src/mmag/agent.py:214 post_deleted` → `_noop` (TODO)

### 27. 测试基础设施薄弱
- 无 `conftest.py`,fixture 散落在 `test_url_analyzer.py:1` 内联,无法跨文件复用
- 无 CI 配置(`.github/workflows/` 不存在),测试无法在 CI 中运行
- `pyproject.toml` 配 `pytest` + `pytest-asyncio` 但未充分利用
- 无覆盖率报告

### 28. discover.py 内部细节
- `src/mmag/discover.py:60-77` 5 个 `_get` 透传方法,均直接调 `MMClient._get` 私有方法(破坏封装)
- 应改为持有 `MMClient` 实例,用公开 API

### 29. prompts.py 当前足够,但未来扩展点
- `src/mmag/prompts.py:_format_dict` 是新加的递归模板替换,**单元测试缺失**

### 30. `time.time()` 与 `time.monotonic()` 混用
- 测耗时用 `monotonic` (好),取时间戳用 `time.time()` (好);但部分 magic conversion (ms ↔ s) 散落多处,容易算错
- `agent.py:236 latest_ms = int(latest_sec * 1000)`
- `agent.py:606 create_at = int(time.time() * 1000)`
- `memory.py:582 datetime.fromtimestamp(create_at / 1000.0 if create_at > 1e12 else create_at)` 启发式判断

---

## 已完成的修复 (历史)

| 时间 | 修复 |
|---|---|
| 2026-06-11 | P0-1 `memory.log_message` 失败信号升级为 error + 4 处调用方感知 + `dropped_messages` 计数 |
| 2026-06-11 | P0-2 `memory_compactor` 摘要 LLM 失败返 None,不再被持久化 |
| 2026-06-11 | P0-3 `mcp_bridge._connect_one` 失败路径显式 `__aexit__` 配对 |
| 2026-06-11 | P0-4 `agent.stop` 4 步关闭全部 try/except |
| 2026-06-11 | P0-5 `agent.stop` 新增 `url_analyzer.close_client()` |
| 2026-06-11 | P3-18 删 7 处零调用死代码 |
| 2026-06-11 | P3-19 触发词提到 `prompts.yml/triggers` 节点 + `PromptManager.get_section` |
| 2026-06-11 | P3-20 `from_bot` / `summary` / `true` props 业务常量提到 `client.py` |
| 2026-06-11 | P3-21 CHANGELOG + README 架构图更新 |
