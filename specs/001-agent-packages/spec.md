# Agent Package v1 规格

## 目标

把 Managed Agent 从运行时 Python 骨架升级为可版本化、可校验、可发布和可回放的配置单元，同时保持 Manifest、Prompt、Schema、Policy 与执行器职责分离。

## 用户故事

### US1：发布 Agent Package

作为平台工程师，我可以发布一个包含 `agent.yml`、版本化 Prompt、输入/输出/Artifact Schema 和 eval 用例的目录。任何未知字段、逃逸路径、非法 Schema 或 Prompt 变量漂移都会在发布前失败，旧版本内容不能被静默覆盖。

### US2：执行有契约的 Agent

作为协同中枢，我在 Agent 执行前校验统一输入 Envelope，只向 Runtime 暴露 Manifest allowlist 中的能力；执行后校验 Artifact 和输出 Envelope，不合格模型输出最多修复一次，仍失败返回稳定的 `INVALID_OUTPUT`。

### US3：复现历史运行

作为审计人员，我可以从输出 provenance 获取 Agent Spec、Prompt、输入/输出 Schema、Policy、Model Policy 版本及 Package/Prompt Hash，定位一次运行使用的准确配置。

### US4：执行权限边界

作为安全管理员，我可以通过版本化 Policy 声明 action、permission、actor、scope 和 role 规则。未知 Policy 或无匹配规则默认拒绝，CapabilityExecutor 在副作用前执行最终裁决。

## 功能要求

- FR-001：Manifest 使用 `mmag.ai/v1` 与 `ManagedAgent`，遵循严格 JSON Schema，所有对象默认拒绝未知字段。
- FR-002：Package 引用必须为包根目录内的相对路径，禁止目录穿越与绝对路径。
- FR-003：Prompt 必须声明 required variables；模板使用未声明变量或声明变量从未使用时拒绝发布。
- FR-004：每个业务 JSON Schema 必须声明 `x-version`，加载时校验其自身合法性。
- FR-005：Registry 支持 name/version 查询、active version 与批量原子加载；已发布同版本内容不可变。
- FR-006：输入、输出和 Artifact 必须在 Agent 边界验证；不合格数据不得进入下一 Agent。
- FR-007：模型输出修复最多一次，修复阶段不可调用 Capability。
- FR-008：Manifest allow 与 deny 不得交叉；Runtime 能力来自已知 Catalog 与 allowlist 的精确交集。
- FR-009：Policy Engine 构造器默认 DENY，Policy 引用必须解析到明确版本。
- FR-010：每个成功输出携带版本 provenance 和 usage。

## 非目标

- v1 不提供在线控制台、远程制品仓库或热更新服务；
- v1 不允许 Agent YAML 内嵌 Secret；
- v1 不把 Prompt 当作权限边界；
- v1 不承诺任意旧版 Agent 的无条件兼容。

## 验收标准

- Link Agent 通过 Package Loader、Registry、输入/输出/Artifact 契约与专属默认拒绝 Policy 执行；
- 非法 Manifest、同版本篡改、非法输入、非法输出与二次修复失败都有离线测试；
- wheel 包含 Agent、Policy 与 Model Policy 声明资源；
- 全量 `make verify` 通过。
