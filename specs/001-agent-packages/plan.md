# Agent Package v1 实施计划

## 架构

```text
Agent Manifest ─┬─> Prompt Registry ───────┐
                ├─> Schema Registry ───────┤
                ├─> Package Registry ──────┼─> Managed Agent Runner
Policy Registry ┴─> Policy Engine ─────────┤          │
Capability Catalog ─> allowlist ───────────┘          v
                                              Runtime / Executor
                                                     │
                                            validated Envelope
```

Manifest 只保存引用和约束。Loader 在发布前编译全部引用并生成不可变指纹；Runner 在每次运行时强制输入、能力、预算、Artifact 和输出边界；CapabilityExecutor 使用当前 Actor/Scope 做最终授权。

## 交付阶段

1. 契约基线：Manifest v1 JSON Schema、不可变模型、严格 Loader；
2. 资产治理：Prompt/Schema Registry、变量契约、版本与 Hash；
3. 发布治理：按 name/version 的原子 Registry 与不可变版本；
4. 运行时治理：统一 Envelope、allowlist、预算、一次输出修复；
5. Policy-as-Code：默认拒绝、版本解析、Capability 执行前授权；
6. 纵向验证：Link Agent Package；
7. 可观测与持久化：把 provenance 写入 AgentRun/Audit；
8. 多 Agent 验证：Research → Presentation 结构化 Artifact handoff；
9. 发布门禁：eval、签名/制品仓库、原子激活与回滚。

## 风险控制

- 全局 Bot 暂时显式使用 ALLOW 兼容策略，新 Package 禁止继承；
- Model Policy v1 先作为版本化声明，下一阶段接入 ModelGateway 路由与模型参数；
- 当前进程内 Registry 不替代后续持久发布记录；
- Schema 只保证结构，资源归属、审批资格、Secret 与预算扣减仍由运行时代码检查。
