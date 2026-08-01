# Agent Package 架构与开发指南

Agent Package 是 Managed Agent 的发布单元，但不是“把所有内容塞进一个 YAML”。MMAG 使用五个协作边界：

```text
Manifest + Prompt Registry + Schema Registry + Policy Engine + Runtime Enforcement
```

## 目录结构

```text
agents/<agent-name>/
  agent.yml
  prompts/v1/{system,task,output_repair}.md
  schemas/{input,output,artifact}.schema.json
  evals/contract-cases.yml

policies/<policy-name>.yml
model-policies/<model-policy-name>.yml
```

`agent.yml` 声明身份、Intent、引用、运行参数、能力可见性、Context、策略和预算。大段 Prompt 与 JSON Schema 保持独立，以便审阅、diff 和回滚。Secret 只能使用运行时 Secret Provider，禁止写入这些目录。

## 加载与发布

`AgentPackageLoader` 会在返回 Package 前完成以下检查：

1. 用 Manifest v1 JSON Schema 拒绝未知字段和错误类型；
2. 拒绝绝对路径、目录穿越和不存在的引用；
3. 编译 Prompt 变量集合，拒绝声明/实际使用漂移；
4. 校验所有业务 JSON Schema，并要求 `x-version`；
5. 拒绝同时 allow/deny 的 Capability；
6. 计算包含 Manifest、Prompt 与 Schema 的 Package Hash。

`AgentPackageRegistry` 按 `name@version` 发布。同一版本允许重复加载相同内容，但内容 Hash 变化会被拒绝；目录批量加载先完整验证再原子替换进程内 Registry，避免半发布。

## 运行时强制

`ContractManagedAgent` 用于包装确定性 Agent；`RuntimePackageAgent` 用于模型型 Agent。两者都在执行前验证统一输入 Envelope，在返回后验证 Agent 专属输出与 Artifact Schema。

模型只生成 `result` 对象，外围 Envelope 由 MMAG 创建，避免模型伪造 usage 或 provenance。首次输出不是合法 JSON 或不满足 Schema 时，Runner 最多调用一次 `output_repair`；修复调用看不到任何 Capability。第二次仍失败时抛出错误码 `INVALID_OUTPUT`，结果不得 handoff。

能力边界分两层：Manifest allowlist 决定 Runtime 能看见什么；Policy Engine 在 CapabilityExecutor 执行前根据真实 action、permission、actor、scope、role 和动态资源参数再次裁决。`resource_arguments` 把模型参数（如 `channel_id`）映射到可信请求上下文资源，参数缺失、资源缺失或值不一致均不匹配。默认 Policy 是 DENY。

## 版本与复现

成功 Envelope 的 `provenance` 包含：

- `agent_spec_version` 与 `package_hash`；
- `prompt_id`、`prompt_version` 与 `prompt_hash`；
- `input_schema_version`、`output_schema_version`；
- `policy_version`、`model_policy_version`。

当前这些字段随结果返回；下一阶段会写入持久 `AgentRun` 与 `AuditEvent`，使重启后的审计和回放不依赖日志。

## 当前状态与下一步

Link Agent 已完成首个纵向切片：启动加载 Package 与版本化只读 Policy，运行时校验输入、输出和 `link_analysis` Artifact。全局 Bot 已迁移到版本化 `global-bot@1.0.0` 默认拒绝 Policy；频道读取/知识写入只能命中当前会话资源，画像默认仅可读取本人，文件外发与 MCP 调用需要审批。

接下来按依赖顺序推进：持久 provenance → Model Policy Registry/ModelGateway 接线 → Research Package → Presentation Package → `research-report` Artifact → 严格 handoff → eval 发布门禁与回滚 → 全局 Bot Package 化。
