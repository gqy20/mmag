# Agent Package v1 任务

## 本次已完成

- [x] Agent Manifest v1 JSON Schema 与严格 Loader；
- [x] 安全相对引用、Prompt Registry 与变量契约；
- [x] Input/Output/Artifact Schema Registry；
- [x] Package/Prompt Hash 与完整版本 provenance；
- [x] name/version Registry、active version 与同版本不可变；
- [x] RuntimePackageAgent：输入校验、能力 allowlist、预算检查、一次输出修复；
- [x] Policy Engine 默认拒绝与版本化 Policy Registry；
- [x] Link Agent Package 纵向接线及专属只读 Policy；
- [x] Package/Policy/Model Policy 资源进入 wheel；
- [x] 聚焦契约测试。

## 下一阶段

- [ ] 把 provenance、输入/输出 Schema 版本写入持久 AgentRun 与 AuditEvent；
- [ ] 实现 Model Policy Registry，并把 route/token/model 参数接入 ModelGateway；
- [ ] 将 Agent Package 发布加入 eval gate、原子激活、回滚和发布人元数据；
- [ ] 为 Runtime 错误实现 Manifest retry 策略，保持副作用调用不自动重试；
- [ ] 将 Research 与 Presentation 从硬编码 `AgentSpec` 迁入 Package；
- [ ] 定义 research-report Artifact 并验证 Research → Presentation 严格 handoff；
- [ ] 删除全局 Bot 显式 ALLOW 兼容策略，所有能力改用请求级 Policy；
- [ ] 将 Package/Policy 根目录配置纳入统一配置 Schema 与启动诊断。
