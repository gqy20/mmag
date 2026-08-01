# Skill Package 架构与开发指南

```text
Agent       = 谁负责：职责、路由、上下文、预算、权限
Skill       = 怎样完成：流程、指令、模板、输入输出契约
Capability  = 执行动作：企业函数/API/MCP Tool
Policy      = 此刻能否执行：actor、scope、resource 与审批
```

Skill 必须依附于已选择 Agent。它可以复用，但不能注册 Agent、扩大 Capability、绕过 Policy，或仅凭
Manifest 获得网络、文件、Python 和 Shell 权限。

## 目录与 Manifest

```text
skills/<name>/
  skill.yml
  SKILL.md
  input.schema.json
  output.schema.json
  <template>.md              # 确实需要按需读取时才添加
  evals.yml                  # 有真实领域质量案例时才添加
```

目录名必须等于 `metadata.name`，版本只保留在 `skill.yml`。`required_capabilities` 必须同时属于
Agent allowlist 且已部署；`optional_capabilities` 只有同时满足这两个条件才进入本次工具集合。

Skill 不拥有可执行脚本。受信 Renderer 属于平台代码，由 Agent、Skill、Policy 与 Execution Profile
共同授权后，以固定命令进入执行平面。

## 选择与原生渐进披露

```text
Request
  → AgentRouter
  → SkillResolver（当前 Agent allowlist 内选择一个）
  → Skill input Schema
  → Capability 交集
  → /skills/<name>/ StateBackend 投影
  → Deep Agents SkillsMiddleware
  → 模型按需 read_file(SKILL.md / template)
  → CapabilityRegistry / Policy / Approval
  → Skill output Schema
  → Agent output Schema / Audit
```

启动时 Loader 校验 `skill.yml`、Schema、SKILL.md 与资源的路径、UTF-8、大小和 Hash。运行时只把本次
选中的精确 Skill Package 投影进 graph 的 StateBackend；SkillsMiddleware 首先只披露元数据，模型在
需要时才读取 SKILL.md 和旁路模板。旧 `load_skill_resource` Capability 与 Prompt 拼接链已经删除。

StateBackend 文件属于当前 LangGraph thread，并随 checkpoint 持久化；它不是宿主机文件权限，也不能
绕过 MMAG Execution Profile。PPT 等执行型 Skill 的平台注册资源仍通过 `SkillContext` 提供可信身份。

## 权限交集

```text
本次可执行 Capability
= Agent allowlist
∩ Skill required/available optional
∩ 当前 Package Policy
∩ Execution Profile command（涉及进程时）
```

交集同时收窄模型 Tool Schema、CapabilityContext 与 GovernanceContext。Skill 即使在 SKILL.md 中要求
额外工具，也不会获得该工具。

## 契约与 provenance

Skill 输入固定投影为 `intent`、`goal`、`parameters`。结果先经过 Skill output Schema，再经过 Agent
result Schema。运行 provenance 包含 Skill name/version、Package Hash、SKILL.md Hash、输入输出 Schema
版本、eval Hash，以及需要时的 Execution Profile Hash；模型不能伪造这些字段。

## 新建 Skill

1. 创建 `skills/<name>/skill.yml`、`SKILL.md` 和输入/输出 Schema；
2. 只声明真正需要的 Capability；
3. 有平台执行需求时声明精确 Execution Profile；
4. 在一个或多个 Agent 的 `skills.allow` 中绑定精确版本；
5. 提升受影响 Agent 的 `metadata.version`；
6. 运行 Package/Skill 定向加载测试后发布。

当前 Skill：

- `web-research@1.2.0`；
- `project@1.2.0`；
- `report@1.3.0`；
- `slides@3.0.0`。
