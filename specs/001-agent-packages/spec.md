# Agent Package v1 规格

## 目标

把 Managed Agent 从运行时 Python 骨架升级为可版本化、可校验、可发布和可回放的配置单元，同时保持 Agent、Skill、Capability、Policy 与执行器职责分离。

## 用户故事

### US1：发布 Agent Package

作为平台工程师，我可以提交一个扁平 Agent Package，其中包含 `agent.yml`、Prompt、输入/输出/Artifact Schema 和 eval 用例。任何未知字段、逃逸路径、非法 Schema 或 Prompt 变量漂移都会在注册前失败；版本保留在 Manifest，历史由 Git 和发布制品保存。

### US2：执行有契约的 Agent

作为协同中枢，我先选择 Agent，再从该 Agent 的 Skill allowlist 选择工作方法；Runtime 只看到 Agent、Skill 和 Policy 共同允许的能力。执行后依次校验 Skill、Artifact 和 Agent 输出契约。

### US3：复现历史运行

作为审计人员，我可以从输出 provenance 获取 Agent、Skill、Prompt、输入/输出 Schema、Policy、Model Policy 版本及内容 Hash，定位一次运行使用的准确配置。

### US4：执行权限边界

作为安全管理员，我可以通过版本化 Policy 声明 action、permission、actor、scope 和 role 规则。未知 Policy 或无匹配规则默认拒绝，CapabilityExecutor 在副作用前执行最终裁决。

## 功能要求

- FR-001：Manifest 使用 `mmag.ai/v1` 与 `ManagedAgent`，遵循严格 JSON Schema，所有对象默认拒绝未知字段。
- FR-002：Package 引用必须为包根目录内的相对路径，禁止目录穿越与绝对路径。
- FR-003：Prompt 必须声明 required variables；模板使用未声明变量或声明变量从未使用时拒绝发布。
- FR-004：每个业务 JSON Schema 必须声明 `x-version`，加载时校验其自身合法性。
- FR-005：Registry 扫描 `agents/*/agent.yml` 并按 name 批量原子加载；目录名必须与 Manifest name 一致。
- FR-006：输入、输出和 Artifact 必须在 Agent 边界验证；不合格数据不得进入下一 Agent。
- FR-007：模型输出修复最多一次，修复阶段不可调用 Capability。
- FR-008：Manifest allow 与 deny 不得交叉；Runtime 能力来自已知 Catalog 与显式 allowlist/pattern 的受限交集。
- FR-009：Policy Engine 构造器默认 DENY，Policy 引用必须解析到明确版本。
- FR-010：每个成功输出携带版本 provenance 和 usage。
- FR-011：Manifest 必须声明 execution Provider 和 routing；未知 Provider、多个默认 Agent 或重复路由拒绝注册。
- FR-012：Capability 根据当前 Package `policy_ref` 动态裁决，缺失 Package Policy Context 默认拒绝。
- FR-013：Agent Manifest 必须以精确版本 allow/deny Skill；未知 Skill 或 Required Capability 超出 Agent allowlist 时原子注册失败。
- FR-014：Skill Manifest、SKILL.md、Schema、资源与 eval 必须形成不可变 Package；Skill script 在 v1 不可直接执行。
- FR-015：SkillResolver 只在已选 Agent 内路由；模型 Tool Schema、CapabilityContext 与 GovernanceContext 同时收窄到 Skill 能力集合。
- FR-016：Skill 输入输出必须通过独立 Schema，Skill version/Package/Instruction/Schema/eval Hash 进入 provenance 与成功运行审计。
- FR-017：Skill 资源采用三级渐进式披露；选中时只注入目录，只有显式 `load_skill_resource` 调用才能读取一个已声明 template/reference。
- FR-018：资源加载必须校验 Package 边界、发布 Hash、UTF-8、数量、单项/总字节与估算 token；scripts 不可读取或执行。
- FR-019：provenance 只记录实际披露资源；LangGraph interrupt/resume 必须保存并恢复资源会话，不能扩大资源集合。

## 非目标

- v1 不提供在线控制台、远程制品仓库或热更新服务；
- v1 不允许 Agent YAML 内嵌 Secret；
- v1 不把 Prompt 当作权限边界；
- v1 不承诺任意旧版 Agent 的无条件兼容。

## 验收标准

- `mmchat` 与 Link Agent 通过 Package Loader、AgentFactory、Router、输入/输出/Artifact 契约与默认拒绝 Policy 执行；
- 非法 Manifest、未知 Provider、路由冲突、非法输入、非法输出与二次修复失败都有离线测试；
- wheel 包含 Agent、Skill、Policy 与 Model Policy 声明资源；
- 全量 `make verify` 通过。
