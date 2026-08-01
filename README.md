# mmag — Mattermost AI Agent

mmag 通过 WebSocket 接入 Mattermost，以版本化 Agent Package、LangGraph Runtime 和默认拒绝的 Capability Policy 运行团队 Agent。

## 核心架构

```text
Mattermost WebSocket
  → Inbox / conversation scheduler
  → application.MessageHandler
  → agent_system.AgentRouter
  → Agent Package contract
  → SkillResolver（Agent 白名单内）
  → LangGraph Runtime（默认）
  → CapabilityRegistry → Policy → Executor
  → Execution Profile / isolated ProcessRunner（仅受管生成能力）
  → Outbox / Delivery
  → Mattermost REST
```

当前主链的关键约束：

- WebSocket 只负责接收；同会话串行、跨会话并发；
- `mmchat` 和 Link Agent 都必须经过 `AgentRouter`，不存在绕过 Router 的全局 Bot 路径；
- `agents/*/agent.yml` 通过可信 Provider 自动构造并原子注册，Application 不感知具体 Agent 名称；
- `skills/*/skill.yml` 注册可复用工作方法；路由先选 Agent，再从该 Agent 白名单选择 Skill；
- Skill 使用三级渐进式披露：先注册 Manifest，选中后加载 `SKILL.md`，模板/参考资料只在 Capability 请求时按 ref 加载；
- LangGraph 是默认 Runtime，使用 SQLite checkpoint 和原生 interrupt/resume；
- `CapabilityRegistry` 是内置能力与 MCP 能力的唯一运行时注册表；
- 本次能力集合是 Agent 权限、Skill 能力声明和当前请求 Policy 的交集；
- Python/CLI 生成能力还必须命中 Agent 与 Skill 绑定的精确 Execution Profile，只能执行固定 argv；
- Prompt、输入/输出 Schema、eval、Skill、Policy 和 Model Policy 都进入版本化 Package 快照；
- Delivery 与 Agent 执行状态独立，投递重试不会重复调用模型；
- 全局 Policy 默认拒绝，文件外发和 MCP 副作用进入审批；
- 失败 Inbox 支持 DLQ 查询和幂等 replay。

更完整的契约见 [Agent Package 指南](docs/AGENT_PACKAGES.md) 与 [Skill Package 指南](docs/SKILL_PACKAGES.md)，实施状态见 [Roadmap](docs/ROADMAP.md)。

当前数字员工包括默认协同 Bot、链接解析、研报、PPT 和项目助理；职责、Skill、权限与交付边界见
[数字员工清单](docs/WORKERS.md)。

## 快速开始

基础服务要求 Python 3.12+、[uv](https://docs.astral.sh/uv/)、Mattermost Bot Token 和 Anthropic 兼容 API Key。启用受控文件生成时，目标 Linux 还必须安装 Bubblewrap 并允许服务账户创建 namespace；`ppt.export_pdf` 额外要求 LibreOffice。依赖缺失时失败关闭，不会回退为宿主机直接执行。

创建 `.env`：

```bash
MM_URL=https://your-mattermost-server
MM_TOKEN=your-bot-token
MM_TEAM_ID=
MM_CHANNEL_ID=

ANTHROPIC_API_KEY=sk-xxx
ANTHROPIC_BASE_URL=
ANTHROPIC_MODEL=claude-sonnet-4-20250514

MAX_CONTEXT_MESSAGES=100
MAX_CONTEXT_CHARS=30000
MEMORY_SUMMARY_INTERVAL=100
MEMORY_CONTEXT_WINDOW=100
PIPELINE_MAX_CONCURRENCY=8
PIPELINE_MAX_PENDING=256
RUNTIME_DEADLINE_SECONDS=120
MODEL_BUDGET_USD=100

USE_SDK_LLM=false
MCP_ALLOWED_TOOLS=
AGENT_PACKAGES_PATH=./agents
SKILL_PACKAGES_PATH=./skills
POLICIES_PATH=./policies
MODEL_POLICIES_PATH=./model-policies
EXECUTION_PROFILES_PATH=./execution-profiles
EXECUTION_RUNTIME_ROOT=.venv
EXECUTION_WORKSPACE_PATH=/tmp/mmag-execution
EXECUTION_WORKSPACE_RETENTION_SECONDS=3600
ARTIFACT_STORE_PATH=./artifacts
```

安装和启动：

```bash
uv sync --locked --dev
make discover
make run
```

Bot 的触发方式：

1. `@bot`、DM、回复 Bot thread：确定性响应；
2. 普通频道消息：由模型根据 Package Prompt 决定响应或输出 `<SILENT>`；
3. “分析链接/Analyze link + URL”：路由到 Link Agent。

## 开发与验证

```bash
make test       # 默认离线测试
make verify     # Ruff、coverage、mypy、wheel smoke
```

默认测试不访问真实 Mattermost、LLM 或公网。wheel smoke 会验证 Agent、Skill、Policy 和 Model Policy 能脱离源码树加载。

系统评估资产位于 `evals/`。静态校验不会访问外部服务；真实用户—Bot E2E 必须同时显式开启环境门禁
和 `--allow-external`，并只使用专用测试账号：

```bash
uv run mmag-eval --root evals validate
MMAG_E2E_ENABLED=1 uv run mmag-eval --root evals run suites/smoke.yml \
  --profile profiles/staging-mattermost.yml --allow-external
```

详细边界见 [评估体系](docs/EVALUATION.md)。

## 项目结构

```text
agents/
  mmchat/
    agent.yml
    prompts/
    schemas/
    evals/
  link/
    agent.yml
    prompts/
    schemas/
    evals/

skills/
  web-research/
    skill.yml
    SKILL.md
    schemas/
    templates/
    evals/

policies/                  # 默认拒绝的 Policy-as-Code
model-policies/            # 模型路由、输出预算和采样策略
execution-profiles/        # 固定 argv、断网、挂载和资源限制
evals/                     # 跨组件系统评估 Scenario、Suite 与 Profile

src/mmag/
  application/             # Composition root、消息编排、上下文/附件、Delivery
  agent_system/            # Agent 契约、Registry、Router、通用 Capability Agent
  agent_packages/          # Manifest、Provider Factory、契约加载与运行时强制
  skill_packages/          # Skill Manifest、Registry、Resolver 与契约校验
  capabilities/            # Capability Spec、Registry、bindings、Executor
  execution/               # Profile Registry、sandbox runner、工作区与 Artifact staging
  runtimes/                # LangGraph 默认 Runtime、可选 Claude SDK Adapter
  control_plane/           # Inbox/Outbox、Lifecycle、Approval、DLQ/replay
  governance/              # Policy、Model Policy、Secret、Quota、运维原语
  evaluation/              # 评估 Loader、Runner、断言、Driver 与报告
  infrastructure/          # 持久化基础设施
  memory.py                # 记忆业务 Facade
  memory_compactor.py      # 长期记忆压缩
  mcp_bridge.py            # MCP discovery → Capability
  client.py                # Mattermost REST Client
  ws_client.py             # Mattermost WebSocket Client
  llm.py                   # Anthropic 模型客户端
  cli.py                   # CLI 入口
```

旧的 `agent.py`、`managed_agents.py`、`tools/`、全局 `prompts.yml` 和手写 Agent loop 入口已经删除；新代码不得重新引入这些兼容层。

## Agent Package 注册规则

```text
agents/<name>/agent.yml
agents/<name>/prompts/...
agents/<name>/schemas/...
agents/<name>/evals/...
```

启动时会：

1. 扫描 `agents/*/agent.yml`，校验目录名与 Manifest name 一致；
2. 严格校验 Prompt 变量、JSON Schema 和 eval case；
3. 解析并校验 `policy_ref`、`model_policy_ref`；
4. 解析 `skills.allow` 精确版本，并校验 Skill Required Capability 不会扩权；
5. 解析 `execution_profiles.allow`，拒绝 Skill 申请 Agent 未允许的 Profile；
6. 将 Prompt/Schema/eval/Skill/Execution Profile/Policy/Model Policy Hash 写入 Package 快照；
7. 根据 `execution.kind/provider` 选择可信 Provider 并构造 Agent；
8. 校验唯一默认 Agent、路由冲突和 Capability allowlist 后原子注册。

版本保留在 `metadata.version`；源码历史交给 Git，发布历史交给制品仓库。CI 比较目标分支，Package 任意内容变化都必须提升 SemVer 并产生新的 Hash。

Skill 同样使用扁平目录和 Manifest 内版本。`SKILL.md` 只描述工作方法，资源读取通过受预算约束的 `load_skill_resource` Capability；`scripts/` 不向模型披露，只能由受信 `ScriptExecutor` 在 Profile sandbox 中按 Hash 和固定 argv 执行。

## 安全边界

- MCP 默认不连接；`MCP_ALLOWED_TOOLS` 必须精确列出 `mcp_<server>_<tool>`；
- MCP stdio 子进程只继承最小运行环境，不继承 Mattermost/模型 Secret；
- Claude SDK 只暴露 in-process MMAG Capability，且每次调用再次匹配当前 Package allowlist；SDK CLI 内置文件/命令工具不对 Bot 开放；
- URL 分析禁用环境代理和自动重定向，每次跳转重新执行 DNS/IP SSRF 校验；
- 频道、用户和文件目标使用可信请求 Context 做资源级匹配；
- Secret 不得写入 Agent Manifest、Prompt、Policy 或日志；
- 不注册通用 Shell、动态 Python、用户命令字符串或 Manifest 自授权入口；
- 未知 Agent、未知 Capability、缺失 Scope 或未命中 Policy 均拒绝。

## 文档

- [Agent Package](docs/AGENT_PACKAGES.md)
- [Skill Package](docs/SKILL_PACKAGES.md)
- [受控执行平面](docs/EXECUTION.md)
- [数字员工清单](docs/WORKERS.md)
- [AI Native 架构](docs/AI_NATIVE_REFACTORING.md)
- [Roadmap](docs/ROADMAP.md)
- [技术债](docs/TECH_DEBT.md)
- [运维指南](docs/OPERATIONS.md)
- [评估体系](docs/EVALUATION.md)
- [架构决策记录](docs/adr/)

## License

MIT
