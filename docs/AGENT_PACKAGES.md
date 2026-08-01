# Agent Package 架构与开发指南

Agent Package 是 MMAG Agent 的不可变发布单元：YAML 声明，Prompt/Schema 提供契约，Policy 和运行时代码负责强制执行。

```text
Agent Manifest
  + Prompt Registry
  + Schema Registry
  + Eval assets
  + Skill Package references
  + Execution Profile references
  + Policy Registry
  + Model Policy Registry
  + Runtime Enforcement
```

## 扁平目录

```text
agents/<agent-name>/
  agent.yml
  system.md                         # 模型 Agent 必需，direct Agent 不需要
  input.schema.json
  output.schema.json
  artifact.schema.json             # 仅产生该类 Artifact 时需要
  evals.yml                         # 仅存在真实质量案例时需要

policies/<policy>.yml
model-policies/<model-policy>.yml
skills/<skill>/skill.yml
execution-profiles/<profile>.yml
```

目录名必须与 Manifest `metadata.name` 完全一致。版本只存在于 `metadata.version`；Git 保存源码历史，构建制品和 Package Hash 保存发布历史，不在源码树复制版本目录。

## Manifest 职责

`agent.yml` 声明：

- Agent 身份、版本和 Intent；
- execution kind/provider 与 routing default/priority/keywords/scope；
- 模型 Agent 的 system Prompt 引用与必需变量；
- 输入、结果和 Artifact Schema；
- Runtime route、轮次、deadline、重试；
- Capability allow/deny；
- Skill allow/deny 精确版本引用；
- Execution Profile allow/deny 精确版本引用；
- Context 读写 scope；
- Policy 和 Model Policy 引用；
- 模型调用、能力调用和成本预算；
- 可产生的 Artifact 类型。

大段 Prompt、JSON Schema 和 eval 不放进 Manifest。Secret 只能由运行时 Secret Provider 解析，禁止出现在 Package 和 Policy 中。

## 加载与自动注册门禁

`AgentPackageLoader` 在构造 Package 前完成：

1. Manifest JSON Schema 校验，拒绝未知字段；
2. 引用路径边界校验，拒绝绝对路径和目录穿越；
3. Prompt 变量编译，拒绝未声明变量、未使用声明和缺失运行时变量；
4. Draft 2020-12 Schema 校验并要求 `x-version`；
5. Capability allow/deny 冲突检查；
6. 可选 eval 文件结构、case ID 和期望字段校验；
7. Manifest、Prompt、Schema 和 eval 内容 Hash 计算。

`AgentPackageRegistry` 随后：

1. 原子加载所有 `agents/*/agent.yml`；
2. 解析 `policy_ref` 和 `model_policy_ref`；
3. 校验 Package route 与 Model Policy route 一致；
4. 把 Policy/Model Policy Hash 合入 Package Hash；
5. 解析 Skill 精确版本，校验 Required Capability 不会扩权，并把 Skill Set Hash 合入 Package Hash；
6. 解析 Execution Profile 精确版本，拒绝 Skill 申请 Agent 未允许的 Profile，并把 Profile Set Hash 合入 Package Hash；
7. `AgentFactory` 根据可信 Provider 创建 Agent；
8. 校验唯一默认 Agent 和重复路由后一次性建立 Registry。

任何 Package 或 Provider 失败，整批 Agent 都不会注册。

## Execution Provider

YAML 不能填写 Python import 路径，只能引用代码注册的稳定 Provider：

```yaml
execution:
  kind: langgraph
  provider: text-v1

routing:
  default: true
  priority: 0
  keywords: []
  requires_url: false
  scopes: ["mattermost:*"]
```

当前 Provider：

- `langgraph/text-v1`：默认文本 Agent，已有 Mattermost Context 时复用准备好的 `RunRequest`；其他入口严格渲染 Package Prompt；
- `langgraph/json-v1`：模型输出 Package 专属 JSON，失败最多修复一次；
- `capability/single-v1`：一个只读 Capability 的确定性 Agent，可按 `source_argument` 从请求提取 URL。

新增普通 Agent 只添加扁平 Package。只有新增执行机制或真实 Capability 时才写 Python。

## 运行时强制

`ContractAgentDecorator` 包装确定性 Agent 或已经准备好 `RunRequest` 的 Runtime Agent；`PackageAgentRunner` 用于让模型直接生成 Package 专属结构化结果。

共同约束：

- 执行前校验统一输入 Envelope；
- Manifest allowlist 决定 Agent 可见的 Capability，支持显式 `mcp_*` 模式；
- `SkillResolver` 只能从当前 Agent 的 Skill allowlist 选择，并进一步收窄模型可见工具与执行上下文；
- Skill 被选中后只注入 `SKILL.md` 和资源目录；独立模板通过 `load_skill_resource` 按步骤披露；
- Capability 执行前由当前 Package 的 `policy_ref` 根据 actor、scope、role 和动态资源参数再次裁决；
- 受管脚本只能由 Agent Profile allowlist、Skill Profile/Capability 声明、Policy 与 Profile command 的交集启动，模型不能提供命令、路径或 Python；
- 返回后校验 result、Artifact 和统一输出 Envelope；
- 预算超限直接失败；
- provenance 来自运行时代码，模型不能伪造。

任务消息和结构修复由平台统一生成，避免每个 Package 复制 `task.md` 和 `output_repair.md`。
`PackageAgentRunner` 对非法模型输出最多执行一次无 Capability 的结构修复；再次失败返回
`INVALID_OUTPUT`，不得把结果交给下游 Agent。

## 输入输出 Envelope

统一输入包含 `task_id`、`run_id`、`intent`、actor、scope、goal、parameters、context refs 和 artifact refs。统一输出包含 status、agent、result、artifacts、sources、warnings、usage 和 provenance。

provenance 当前记录：

- Agent spec version 与最终 Package Hash；
- Prompt ID/version/hash；
- 输入输出 Schema version；
- Policy version/hash；
- Model Policy version/hash；
- eval hash。
- Skill set hash；选中 Skill 时还记录 Skill version、Package/Instruction/Schema/eval Hash；
- Execution Profile set hash；使用时记录精确 Profile ref/Hash、运行时摘要、脚本/可执行文件/argv Hash；
- 实际披露 Skill Resource 的 ref、Hash、字节、估算 token 和聚合 Hash。

## 当前 Package

- `mmchat@1.3.0`：Mattermost 默认对话 Agent；允许 `web-research@1.1.0`；
- `link@1.3.0`：直接执行确定性链接分析，不再伪造 Skill 和 Prompt；
- `report@1.1.0`：结构化研报 Agent；绑定只读 `report@1.1.0` Skill；
- `ppt@2.3.0`：演示文稿 Agent；绑定 `slides@2.3.0` 与 `ppt@2.1.0` Execution Profile；
- `project@1.1.0`：项目助理 Agent；绑定 `project@1.1.0`，共享知识写入需要审批。

模型 Package 具备 system Prompt；所有 Package 保留输入/输出契约、Policy、预算和 provenance。
Package eval 只在存在领域质量案例时添加，通用 Loader/Schema 不变量由平台测试覆盖。

## 尚未完成的生产门禁

- eval 目前做静态契约校验，尚未在发布时执行质量 case；
- 成功运行的完整 provenance 已写入 AuditEvent；失败阶段和细粒度 usage 仍需归一化；
- Model Policy 已严格加载并参与 Hash，但 `model_class` 与 temperature 还没有驱动多模型路由；
- Report → PPT 的 research report Artifact 持久化与严格 ref handoff 尚未实现；
- PPTX 与直接 PNG 预览链路已实现并完成本机 smoke；`ppt.shell` 当前是无文件系统隔离的 Demo 权限，生产化前必须替换为隔离 Runner。

这些缺口不会通过兼容层或假实现掩盖，统一记录在 [TECH_DEBT.md](TECH_DEBT.md) 和 [ROADMAP.md](ROADMAP.md)。

Skill 的包结构、选择规则和安全边界见 [Skill Package 指南](SKILL_PACKAGES.md)。
固定 argv、隔离、Artifact staging 与部署要求见 [受控执行平面](EXECUTION.md)。
