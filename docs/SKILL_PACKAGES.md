# Skill Package 架构与开发指南

MMAG 将四个概念严格分层：

```text
Agent       = 谁负责：职责、路由、上下文、预算、权限
Skill       = 怎样完成：可复用流程、指令、模板、输入输出契约
Capability  = 执行动作：内置函数、企业 API 或 MCP Tool 的统一适配
Policy      = 此刻能否执行：actor、scope、resource、审批与默认拒绝
```

Agent 与 Skill 是多对多关系，但一次 Skill 只能在已选择 Agent 的白名单内激活。Skill 不能自行注册 Agent、增加 Capability、绕过 Policy 或直接获得网络、文件和 Shell 权限。

## 扁平目录

```text
skills/<skill-name>/
  skill.yml
  SKILL.md
  schemas/{input,output}.schema.json
  templates/...
  references/...
  scripts/...
  evals/contract-cases.yml
```

目录名必须等于 `metadata.name`。版本只保留在 `skill.yml` 的 `metadata.version`，不建立 `versions/` 层级。Git 保存源码历史，制品仓库保存发布历史，Package Hash 标识不可变内容。

## Manifest

```yaml
api_version: mmag.ai/v1
kind: Skill

metadata:
  name: web-research
  version: 1.0.0
  description: Plan, verify, and synthesize web research.

spec:
  instruction_ref: SKILL.md
  input_schema_ref: schemas/input.schema.json
  output_schema_ref: schemas/output.schema.json

  activation:
    intents: [research, compare, report]
    keywords: [调研, 竞品, research]
    priority: 50

  required_capabilities: [analyze_link, load_skill_resource]
  optional_capabilities: [search_knowledge]
  execution_profiles: []

  resources:
    templates: [templates/report.md]
    references: [references/source-quality.md]
    scripts: []

  disclosure:
    max_resources: 3
    max_resource_bytes: 16384
    max_total_bytes: 32768
    max_estimated_tokens: 8000
```

`required_capabilities` 必须全部属于 Agent 的 Capability allowlist，且部署时真实存在；缺少任意一项则拒绝选择。`optional_capabilities` 只在同时被 Agent 允许且已部署时加入本次能力集合。

`templates/` 和 `references/` 是可披露的 UTF-8 文本资源；`scripts/` 会被校验和哈希，但永不进入资源目录或模型上下文。脚本只有同时绑定已注册 Execution Profile、窄口 Capability，并通过 Agent 与 Policy 后，才能由 `ScriptExecutor` 在 sandbox 中以固定 argv 执行；`SKILL.md`、脚本路径或 Manifest 都不能直接启动进程。

## 三级渐进式披露

```text
Level 1：启动时加载 skill.yml、Schema 和资源元数据/Hash
Level 2：Skill 被选中后加载 SKILL.md，并只注入资源目录
Level 3：模型明确调用 load_skill_resource(ref) 后加载一个模板或参考资料
```

启动时会读取资源字节以校验路径、UTF-8、大小和 Hash，但不会保留内容或注入模型。运行时首次请求才再次校验 Hash，并以内容 Hash 缓存不可变文本。资源目录只披露 ref、kind 和字节数，不披露内容。

`SkillResourceSession` 为每次 Run 单独维护：

- 已加载资源数量；
- 单资源和累计字节；
- 基于 UTF-8 字节数的保守 token 估算；
- 每个实际加载资源的 ref、kind 和 Hash；
- 聚合 Resource Hash。

重复加载同一个 ref 不重复扣减预算。未被加载的资源不会出现在 Prompt、usage 或运行 provenance。资源会话进入 LangGraph interrupt，审批 resume 后只恢复之前实际披露的 ref，并继续沿用相同预算与 Hash 校验。

## Agent 绑定

Agent Manifest 显式绑定带版本的 Skill：

```yaml
spec:
  skills:
    allow:
      - web-research@1.0.0
    deny: []
```

启动时 `AgentPackageRegistry` 解析所有 Skill 引用，校验 Required Capability 不会扩权，并把 Skill Package Hash 合入 Agent Package Hash。未知 Skill、版本不匹配或能力不满足会让整批 Agent 原子注册失败。

## 选择与执行链

```text
Request
  → AgentRouter
  → SkillResolver（只看该 Agent 的 allowlist）
  → Skill input Schema
  → 指令 Hash 校验与按需加载
  → 只注入资源目录
  → load_skill_resource 按步骤加载单个资源
  → Agent Runtime / LangGraph
  → CapabilityRegistry
  → Package Policy / Approval
  → Skill output Schema
  → Agent output Schema
  → AuditEvent + provenance
```

自动选择根据 `activation.intents`、`keywords` 和 `priority` 排序；API 调用也可以在 `AgentRequest.requested_skill` 中精确请求名称或 `name@version`，但仍然不能越过 Agent allowlist。

本次模型可见并可执行的能力为：

```text
Agent 已解析能力
∩ Skill required/available optional
∩ 当前请求的 Package Policy 决策
∩ Execution Profile command
```

运行时会同时收窄：

- 传给模型的 Tool Schema；
- `CapabilityContext.allowed_capabilities`；
- `GovernanceContext.allowed_capabilities`；
- LangGraph interrupt/resume 中保存的治理快照。

因此，即使模型尝试调用 Agent 原本拥有、但当前 Skill 未声明的 Capability，也会在执行前失败。

`load_skill_resource` 自身也是只读 Capability：必须同时被 Agent 和 Skill 允许，并通过当前 Package Policy。它只能访问当前 `SkillResourceSession`，不能指定其他 Skill，也不能读取未声明路径、绝对路径、目录穿越或 scripts。

## 契约与 provenance

Skill 输入固定投影为 `intent`、`goal` 和 `parameters`，选择前执行 JSON Schema 校验。模型/Agent 结果先经过 Skill output Schema，再经过 Agent result Schema；不合格结果不得进入下游 handoff。

成功运行的 provenance 追加：

- Skill name/version；
- Skill Package Hash；
- SKILL.md Hash；
- Skill input/output Schema version；
- Skill eval Hash。
- Skill 绑定的精确 Execution Profile provenance Hash；真正执行时再追加 Profile、脚本、可执行文件和 argv Hash。
- 实际加载资源数量、总字节、估算 token、聚合 Hash 和逐资源清单。

这些字段与 Agent、Prompt、Policy、Model Policy 和 Schema 快照一起写入 `agent.run` AuditEvent。模型只能产生业务结果，不能填写 provenance。

## 新建 Skill

1. 创建 `skills/<name>/skill.yml` 和 `SKILL.md`；
2. 定义输入/输出 JSON Schema，并声明 `x-version`；
3. 添加至少一个 `evals/*.yml` 契约 case；
4. 只声明完成流程真正需要的 Capability；
5. 需要受管脚本时，在 Skill 声明精确 `execution_profiles`，同时让 Agent 显式允许相同 Profile；
6. 在一个或多个 Agent 的 `skills.allow` 中绑定精确版本；
7. 提升相关 Agent 的 `metadata.version`；
8. 通过 Package 加载和定向契约测试后发布。

当前包：

- `web-research@1.0.0`：默认会话中的轻量研究；
- `link-read@1.0.0`：确定性链接解析契约；
- `report@1.0.0`：证据账本式研报流程；
- `slides@1.1.0`：受来源约束的演示叙事，并通过 `ppt@1.0.0` 受控执行平面生成 PPTX/PDF Artifact；
- `project@1.0.0`：项目计划和状态评估。

绑定关系与实际权限见 [数字员工清单](WORKERS.md)。
执行隔离和发布要求见 [受控执行平面](EXECUTION.md)。
