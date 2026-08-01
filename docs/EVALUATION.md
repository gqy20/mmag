# Evaluation Framework

MMAG 的评估分为 Package 发布评估、离线系统测试和显式真实 Mattermost E2E。三者共享版本、Hash、
结构化结果和默认拒绝外部访问的原则，但不混用资产与凭据。

## 目录与职责

```text
agents/<name>/evals/       # Agent 版本的 contract/quality 发布资产
skills/<name>/evals/       # Skill 版本的 contract/quality 发布资产
evals/                     # 跨 Mattermost、Agent、审批、Artifact 的系统场景
src/mmag/evaluation/       # Loader、Runner、Driver、断言和报告
tests/evaluation/          # 默认离线的框架测试与显式 external smoke
.eval-runs/                # 本地评估报告，不进入 Git
```

顶层资产采用 `mmag.ai/eval/v1`，并由 Draft 2020-12 Schema 拒绝未知字段。Suite 只引用 `evals/`
根目录内的 Scenario；相对路径不能越界。每个 Profile、Suite 和 Scenario 都计算 SHA-256 并进入结果。

## 运行边界

静态验证只解析和校验资产，不导入应用全局 Config，也不访问 Mattermost、LLM 或公网：

```bash
uv run mmag-eval --root evals validate
```

真实 E2E 需要：

1. 专用 Mattermost 测试频道和低权限测试账号；
2. `.env` 或 CI Secret 中提供 Profile 引用的变量；
3. `MMAG_E2E_ENABLED=1`；
4. 命令行显式传入 `--allow-external`；
5. 远程 Mattermost 使用 HTTPS，或仅在受信 localhost 使用 HTTP；
6. Bot 已独立启动并监听目标频道。

```bash
uv run mmag-eval --root evals run suites/smoke.yml \
  --profile profiles/staging-mattermost.yml \
  --allow-external
```

`staging-mattermost` 使用 `MM_USERNAME` / `MM_PASSWORD` 作为 requester；审批和越权套件按需读取独立
approver/unauthorized 账号。缺失的非当前 Case 账号不会被读取。登录 Session 在 Case 退出时注销。

## 观察与断言

Mattermost Driver 使用用户账号发出显式 `@bot` 消息，以原 post ID 关联 MMAG Run，轮询同一 Thread
直到出现成功、失败、耗尽或等待审批状态。需要审批的 Scenario 可以指定 decision 和 actor；Driver
只发送现有文本批准/拒绝命令，不绕过审批服务。

确定性断言覆盖：

- Response kind、终态和 Thread 一致性；
- 默认用户界面不得直接暴露原始 JSON；
- 必含/禁含文本和 Secret 标记；
- 是否进入审批以及未授权审批是否被拒绝；
- Mattermost 文件数量；
- 可选 AgentRun、Task 和 Delivery 只读状态；
- Case deadline。

安全断言失败单独计入 `security_violation_count`，不能被平均质量分抵消。完整响应只保存脱敏后的有限
excerpt 和 SHA-256；报告不保存密码、Token 或 Session。

## Suite 与报告

- `suites/smoke.yml`：单账号用户—Bot Thread 主链；
- `suites/mattermost-e2e.yml`：主链加 PPT 审批与 Artifact 交付；
- `suites/security.yml`：独立未授权账号尝试批准他人的请求。

每次运行在 `.eval-runs/<evaluation-run-id>.json` 原子生成报告，记录资产 Hash、Profile ID、Case 结果、
MMAG Run ID、响应 Hash、断言、耗时和错误码。报告文件是本地运行证据，不是正式发布记录。

## 后续发布门禁

当前实现完成了严格资产、真实 API Driver、确定性断言、Suite 阈值和原子报告。尚未完成：

- 激活 Agent/Skill 前实际执行其 contract/quality case；
- 数据集、评分器和人工评审版本化；
- EvaluationRun 与 AgentRun、Package snapshot 在控制面持久关联；
- 报告作为 `evaluation_report` Artifact 原子发布；
- 失败评估阻止制品发布以及按 Package Hash 回滚；
- 少量 Web/Desktop/Mobile 浏览器 smoke。
