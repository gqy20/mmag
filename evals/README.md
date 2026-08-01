# MMAG System Evaluations

这里保存跨 Agent、Mattermost、审批、Artifact 和 Delivery 的版本化系统评估资产。

- `agents/*/evals` 与 `skills/*/evals` 属于单个 Package，进入对应 Package Hash；
- 顶层 `evals/` 验证部署后的组合行为，不进入 Bot 运行时 Package，也不保存 Secret；
- `profiles/` 只引用环境变量名称；用户名、密码、Token 和真实文件不允许写入 YAML；
- `.eval-runs/` 是本地运行结果目录，默认不进入 Git。

静态校验不会访问外部服务：

```bash
uv run mmag-eval --root evals validate
```

真实 Mattermost Smoke 必须同时提供命令行授权与环境开关：

```bash
MMAG_E2E_ENABLED=1 uv run mmag-eval --root evals run suites/smoke.yml \
  --profile profiles/staging-mattermost.yml --allow-external
```

远程地址必须使用 HTTPS；只有 `localhost`、`127.0.0.1` 和 `::1` 可以使用 HTTP。
完整契约与运行边界见 [Evaluation Framework](../docs/EVALUATION.md)。
