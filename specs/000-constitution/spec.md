# MMAG 工程宪章

## 核心原则

1. **安全默认值**：未知 Agent、Capability、Policy、Scope 或身份默认拒绝；兼容例外必须显式装配并记录退出计划。
2. **契约先于实现**：跨 Runtime、Agent、Capability 和 Artifact 的边界必须有稳定类型、Schema、错误码与离线测试。
3. **声明与执行分离**：YAML 描述意图，JSON Schema 验证结构，运行时代码执行权限、预算、状态和副作用守卫。
4. **单一事实来源**：Prompt、Schema、Capability、Policy 各自只有一个权威注册来源，其他表示由它派生。
5. **可复现运行**：每次 Run 必须能够关联 Agent、Prompt、Schema、Policy 和 Model Policy 的版本或内容指纹。
6. **渐进式模块化单体**：以可验证的纵向切片推进；没有容量、隔离或团队边界证据前不拆微服务。

## 工程门禁

- Python 代码通过 Ruff、mypy 和默认离线 pytest；
- 新增行为必须有正常、失败和安全边界测试；
- 生产依赖必须在 `pyproject.toml` 与 `uv.lock` 中直接声明；
- 配置或运行行为变化同步更新 README、CHANGELOG 与对应架构文档；
- Secret 只能保存名称引用，不能写入 Manifest、Prompt、Policy、日志或测试夹具。

## 兼容策略

兼容层必须位于边界，不能污染新核心模型。每个兼容例外都要说明调用方、风险和删除条件；新 Agent 不得依赖全局 Bot 的宽松 Prompt 或默认允许 Policy。
