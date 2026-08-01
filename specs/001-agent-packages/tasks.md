# Agent Package v1 任务

## 本次已完成

- [x] Agent Manifest v1 JSON Schema 与严格 Loader；
- [x] 安全相对引用、Prompt Registry 与变量契约；
- [x] Input/Output/Artifact Schema Registry；
- [x] Package/Prompt Hash 与完整版本 provenance；
- [x] 扁平 name Registry、Manifest version 与内容 Hash；
- [x] `ContractAgentDecorator` / `PackageAgentRunner`：输入校验、能力 allowlist、预算检查、一次输出修复；
- [x] Policy Engine 默认拒绝与版本化 Policy Registry；
- [x] Link Agent Package 纵向接线及专属只读 Policy；
- [x] Package/Policy/Model Policy 资源进入 wheel；
- [x] `agents/<name>/agent.yml` 扁平目录、eval 静态门禁与治理 Hash；
- [x] `execution/routing`、可信 Provider Registry、AgentFactory 和自动注册；
- [x] mmchat Prompt/Schema/Policy Package 化，默认消息主链接入 Router；
- [x] Capability 按当前 Package Policy 动态授权，Link 改为通用 Capability Agent；
- [x] 删除全局 Prompt、旧 Agent/Tool 模块和硬编码占位 Agent；
- [x] Model Policy Registry、严格引用和 route 校验；
- [x] Skill Package v1 Manifest、Loader、Registry、Resolver、Schema 与资源 Hash；
- [x] Agent-Skill 精确版本绑定和 Required Capability 不扩权门禁；
- [x] Agent → Skill 路由、工具可见性/执行上下文收窄和 Skill provenance 审计；
- [x] 首个 `web-research@1.0.0` Skill 与 `mmchat@1.1.0` 绑定；
- [x] Skill Manifest → SKILL.md → template/reference 的三级渐进式披露；
- [x] ResourceLoader、Hash Cache、数量/字节/token 预算与实际加载 provenance；
- [x] `load_skill_resource` 进入 Capability/Policy 主链，scripts 保持不可读取和不可执行；
- [x] LangGraph interrupt/resume 保存并恢复 Skill Resource Session；
- [x] 聚焦契约测试。

## 下一阶段

- [x] 把成功运行的 provenance、输入/输出 Schema 版本写入 AuditEvent；
- [ ] 将 provenance 与 AgentRun 终态、失败、审批 resume 和 replay 原子持久化；
- [ ] 把 Model Policy 的 token/model/temperature 参数接入 ModelGateway；
- [ ] 将 Agent Package 发布加入可执行 eval gate 和发布人元数据；
- [ ] 为 Runtime 错误实现 Manifest retry 策略，保持副作用调用不自动重试；
- [ ] 实现真实 Research 与 Presentation Package；
- [ ] 定义 research-report Artifact 并验证 Research → Presentation 严格 handoff；
- [x] 删除全局 Bot 显式 ALLOW 兼容策略，所有能力改用请求级 Policy；
- [x] 将 Package/Policy/Model Policy 根目录纳入统一配置与 wheel smoke。
