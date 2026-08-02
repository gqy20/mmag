# Agent Package 架构与开发指南

Agent Package 是 MMAG Agent 的不可变发布单元。YAML 只声明身份与边界；Deep Agents 执行模型循环，
Schema、CapabilityExecutor 和 Policy 负责强制治理。

## 目录

```text
agents/<name>/
  agent.yml
  system.md                 # agent 模式需要；direct 模式不需要
  input.schema.json
  output.schema.json
  artifact.schema.json      # 仅在产生该 Artifact 时存在
  evals.yml                 # 仅在有真实领域案例时存在
```

目录名必须等于 `metadata.name`。版本只写在 Manifest 的 `metadata.version`；源码树不复制
`versions/` 目录，发布身份由精确版本与 Package Hash 共同表达。

## 两种运行语义

模型 Agent 使用默认 `agent` 模式，不在 YAML 中声明 Deep Agents、LangGraph Provider 或 Python import：

```yaml
spec:
  runtime:
    route: default
    max_turns: 12
    timeout_seconds: 600
    retry: {max_attempts: 2}
```

确定性只读 Agent 显式声明 `direct`：

```yaml
spec:
  runtime:
    mode: direct
    capability: analyze_link
    source_argument: url
    route: default
    max_turns: 1
    timeout_seconds: 120
    retry: {max_attempts: 2}
```

`direct` 必须只暴露一个真实 READ Capability；默认 Agent 必须使用 `agent`。旧
`text-v1/json-v1/single-v1` Provider 与 Provider Registry 已删除，不保留兼容别名。

## 加载与注册门禁

Loader/Registry 在启动时原子完成：

1. Manifest JSON Schema、未知字段和安全相对路径校验；
2. Prompt 变量、Draft 2020-12 Schema 与 `x-version` 校验；
3. Capability allow/deny、Policy、Model Policy 和 route 校验；
4. Skill/Execution Profile 精确版本与“不得扩权”校验；
5. Prompt、Schema、Policy、Skill、Profile 和 Package Hash 计算；
6. 唯一默认 Agent、目录名和路由冲突校验；
7. `AgentFactory` 只按 `agent/direct` 创建运行对象。

任意 Package 失败时整批注册失败。Secret、模型端点、Shell 命令和 Python 路径都不能写进 Agent
Manifest。

## 模型 Agent 运行链

```text
AgentRouter
  → SkillResolver
  → ContractAgentDecorator
  → DeepAgentProvider（构造受约束 RunRequest）
  → ModelGateway（Quota）
  → DeepAgentRuntime
      ├── ChatAnthropic
      ├── StateBackend + SkillsMiddleware
      ├── Capability Tools
      ├── response_format
      └── LangGraph checkpoint / interrupt / resume
  → Skill/Agent output Schema
  → Envelope + Audit + Delivery
```

运行时强制：

- 模型可见工具是 Agent allowlist 与选中 Skill Capability 的交集；
- 每次 Tool 调用仍进入 CapabilityRegistry、动态 Policy 和 CapabilityExecutor；
- 需要审批的 Tool 由 Deep Agents 原生 HITL 在副作用前暂停；
- SQLite checkpoint 与持久化 runtime snapshot 支持跨进程恢复同一 graph；
- 选中 Skill 投影到 StateBackend 的 `/skills/<name>/`，由 SkillsMiddleware 渐进读取；
- `response_format` 直接产生结构化结果，不再执行第二套 JSON repair loop；
- 返回后校验 Skill output、Agent result、Artifact 和预算；
- provenance 由平台注入，模型不能填写。

## 当前 Package

- `mmchat@2.1.0`：默认 Mattermost 会话 Agent，允许 `web-research@1.2.0` 和 crawl 搜索子集；
- `link@2.0.0`：`direct` 链接分析；
- `report@2.1.0`：绑定 `report@1.3.0` 和完整 crawl 工具集；
- `ppt@3.1.0`：绑定 `slides@3.1.0` 与 `ppt@2.2.0` Execution Profile；
- `project@2.0.0`：绑定 `project@1.2.0`。

## 新建 Agent

1. 新建 `agents/<name>/agent.yml` 和输入/输出 Schema；
2. 模型 Agent 添加 `system.md`，普通情况不写 `runtime.mode`；
3. 声明最小 Capability、Skill、Policy、Model Policy 与预算；
4. 需要执行平面时同时允许精确 Execution Profile；
5. 添加真实 eval 时才创建 `evals.yml`；
6. 运行 Package 定向加载测试并发布。

MCP Server 与平台工具清单统一放在根目录 `.mcp.json`；Agent 只在 `capabilities.allow` 中分配 `mcp_<server>_<tool>` 或受控模式，不复制连接、Secret 和启停配置。

Skill 规则见 [SKILL_PACKAGES.md](SKILL_PACKAGES.md)，Deep Agents 实现边界见
[DEEP_AGENTS_REFACTORING.md](DEEP_AGENTS_REFACTORING.md)。
