# ADR-0009：分层评估资产与显式外部 E2E

- 状态：Accepted
- 日期：2026-08-01

## 背景

Agent 与 Skill Package 已包含静态 contract case，但它们不能验证真实 Mattermost 登录、WebSocket
入口、Agent 路由、LangGraph、审批、Artifact、Outbox 和用户展示组成的完整链路。把真实账号和外部
服务直接接入默认 pytest 又会破坏离线、可重复和 Secret 隔离边界。

## 决策

1. `agents/*/evals` 和 `skills/*/evals` 继续作为单个 Package 的发布资产并进入 Package Hash；顶层
   `evals/` 保存跨组件的系统 Scenario、Suite、Profile 和 JSON Schema，不复制 Package case。
2. `src/mmag/evaluation` 提供平台无关模型、严格 Loader、Runner、确定性断言和原子 JSON Report；
   Mattermost 只是显式 Driver，不进入 Agent Runtime、Capability 或 Policy。
3. 真实 Mattermost 请求要求命令行 `--allow-external` 与 Profile 的 `enabled_env` 同时开启；默认 pytest
   继续排除 `external`。Profile 只保存环境变量名称，不保存用户名、密码、Token 或 Session。
4. 用户密码只用于专用测试账号登录。远程 Mattermost 必须使用 HTTPS；受信 localhost HTTP 是唯一
   例外。Session 在单个 Case 结束时注销，报告对 Secret 字段和值再次脱敏。
5. 黑盒结果通过原 Mattermost post ID 关联 `mattermost:<post_id>`；同机测试可选使用 SQLite 只读
   Observer 检查 `run:<post_id>`、Task 和 Delivery 状态，不允许评估代码修改控制面数据库。
6. pytest 验证评估引擎本身和少量显式外部 smoke；重复运行、Suite 阈值、报告与后续发布门禁由
   `EvaluationRunner` 负责，不能用 LLM Judge 替代安全、权限、幂等和 Schema 的确定性断言。

## 结果与边界

系统可以版本化描述并执行用户—Bot 全流程，同时保持默认 CI 离线和凭据隔离。第一版持久化原子 JSON
报告；把 EvaluationRun、评分器版本、发布人和报告 Artifact 纳入控制面及 Package 激活事务仍属于
Roadmap。浏览器 UI 自动化不进入第一版，Mattermost API E2E 先覆盖真实业务链路。
