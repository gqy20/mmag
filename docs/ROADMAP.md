# Roadmap

> 项目当前处于 **0.1.x 阶段** — 快速迭代,小版本发布频繁,主要做稳定性补丁、错误处理、可观测、测试与文档。
> 0.1.0 已发布(2026-06-11),后续版本号 (0.1.1, 0.1.2, ...) 按完成里程碑的实际内容签发,不强绑定。
> 详细的"问题清单"见 [`TECH_DEBT.md`](./TECH_DEBT.md),每个里程碑范围都从那里引用。

---

## 当前阶段: 0.1.x — 稳定性与质量

不做大架构改动,不做破坏性 API 变更。每个里程碑完成后直接发版。

### 0.1.1 — 稳定性补丁

**目标**: 网络抖动与认证失效场景下,Bot 不静默失能

**范围** (引自 `TECH_DEBT.md` #1, #2):
- MMClient REST 调用加重试(指数退避)
- WebSocket 401 告警(运维侧可感知 Token 过期)
- WebSocket 重连熔断(连续失败 N 次后停,不再无限重连)

**不做**:
- 任何架构拆分(留给 0.2.0)
- LLM 异常分类(留给 0.1.2)
- 测试基础设施(留给 0.1.3)

**验收**:
- 模拟网络抖动 1 分钟内自动恢复
- Token 过期 5 分钟内有日志告警
- 0.1.1 版本起无"P0 级"未修复 bug

---

### 0.1.2 — 错误处理与可观测

**目标**: 失败可定位,失败可恢复

**范围** (引自 `TECH_DEBT.md` #3, #4, #13):
- LLM 异常分类(`LLMTimeout` / `LLMRateLimit` / `LLMRejected` / `LLMUnavailable`),agent 层按类型做不同 fallback
- 错误日志统一加上下文(`channel_id` / `post_id` / `batch_size` / `model` 等)
- 统一错误处理规范文档(进 `docs/`)
- `Agent.stats` 增 `llm_failures` / `mm_failures` 等指标字段
- LLM 限流时指数退避重试(SDK 默认 0 次,改为最多 3 次)

**不做**:
- 完整监控/告警系统(留给 1.0.0)
- 性能基线(留给 1.0.0)

**验收**:
- LLM 限流 1 分钟内自动恢复,无连续 10 条相同错误提示
- 所有 `log.error` 含可定位上下文字段
- 至少有 1 个 `docs/ERROR_HANDLING.md` 规范

---

### 0.1.3 — 测试基础设施与覆盖率

**目标**: 改代码有安全感

**范围** (引自 `TECH_DEBT.md` #5, #27, #29):
- 补 `conftest.py`,fixture 跨文件复用
- 补 4 条关键链路 e2e 测试:
  - WebSocket → 触发 → LLM → 工具调用 → 回复
  - LLM `agent_loop` 多轮工具调用
  - 消息路由触发判定
  - Backfill 分页
- 补核心模块单元测试(`agent.py` / `memory.py` / `ws_client.py` / `mcp_bridge.py`)
- 引入 `pytest-cov`,覆盖率 ≥ 50%
- 引入 mypy(仅 src/ 目录,先 strict=False)
- 配 GitHub Actions CI(跑 pytest + ruff + mypy)

**不做**:
- 覆盖率到 80%(留给 0.1.4 或 1.0.0)
- mypy strict 模式(留给 1.0.0)

**验收**:
- CI 跑通,所有 PR 必过测试 + lint + type check
- 核心模块单测覆盖率 ≥ 50%
- 4 条 e2e 链路全部覆盖

---

### 0.1.4 — 文档与开发者体验

**目标**: 新人 30 分钟能上手,贡献者 1 小时内能跑通测试

**范围** (引自 `TECH_DEBT.md` 部分文档相关项):
- `docs/ARCHITECTURE.md` 详细架构图(分层 / 数据流 / 关键时序)
- `docs/ERROR_HANDLING.md` 错误处理规范(从 0.1.2 输出)
- `docs/CONTRIBUTING.md` 贡献指南(开发环境 / 测试 / 提 PR 流程)
- `docs/API.md` 工具/MCP 集成参考
- 处理 2 处 `TODO`(`post_edited` / `post_deleted` 当前是 `_noop`,决定是否实现)

**不做**:
- 自动生成 API 文档(留给 1.0.0)
- 国际化(留给 1.0.0)

**验收**:
- `docs/` 至少包含: `ARCHITECTURE` / `ERROR_HANDLING` / `CONTRIBUTING` / `API`
- README 链接到所有 docs
- TODO 清零或显式延期

---

## 远期(不锁时间,只列方向)

### 0.2.0 — 架构清理

不做时间承诺,大概率在 0.1.x 全部完成后启动。

**核心**: 拆 God class,统一风格
- 拆 `agent.py`(629 行,7 职责)→ `MessageRouter` / `ContextBuilder` / `TriggerJudge` / `ResponseSender`
- 拆 `memory.py`(1012 行,7 Repository)→ 5 个独立 Repository
- 拆 `url_analyzer.py`(725 行)→ `ssrf_guard` / `github_api` / `html_extractor` / `cache`
- `MMClient` 改 `httpx.AsyncClient`,消除 `requests` 依赖
- `__init__.py` 不再 eager import,`from mmag import Config` 轻量
- 依赖清理:`mcp[cli]` → `mcp`,`requests` 删除
- 配置加 pydantic 校验

**风险**: 大,需要 e2e 测试保底(0.1.3 必须先完成)

### 1.0.0 — 生产就绪

**核心**: 给运维/客户信心
- 全套监控告警(metrics 收集 / 异常告警 / 性能基线)
- 真实环境压测报告(并发 / 长跑 / 内存增长)
- mypy strict 模式
- 测试覆盖率 ≥ 80%
- 完整 changelog + 迁移指南
- 安全审计(prompt 注入 / 工具越权 / 密钥管理)

---

## 修订规则

- 每个里程碑完成后,更新本文件 + `CHANGELOG.md`
- 里程碑顺序可调整(如 0.1.2 报错少可优先做 0.1.3)
- 新增想法请先加到 `TECH_DEBT.md`,再决定进哪个里程碑
- 远期方向(0.2.0 / 1.0.0)只增不删,完成后才移出
