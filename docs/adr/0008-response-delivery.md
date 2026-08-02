# ADR-0008：平台无关响应与 Mattermost 交付边界

- 状态：Accepted
- 日期：2026-08-01

## 背景

Agent Package 已能返回结构化 `result`、Artifact 和 provenance，但 Mattermost 入口曾将结果降级为
一段自由文本。线程、长消息、附件、更新消息和交互动作也没有进入 Outbox，`send_file` 还在
Capability handler 内直接上传并发帖。这使结构化结果不可稳定展示，外部副作用无法复用 Delivery 的
幂等、重试和审计边界。

## 决策

1. Agent 只返回平台无关的 `AgentOutput`。应用层 `ResponsePresenter` 将结果投影为不可变
   `ResponseView`；Mattermost `props`、按钮和 Markdown 不进入 Prompt 或 Agent Schema。
2. `MattermostRenderer` 确定性生成受限、转义后的 Markdown、来源、Artifact 摘要和可选 action；
   长消息在行边界分段，并在分段处闭合、重开代码围栏。
3. ack、审批、错误、最终结果和附件都使用原消息的 Thread root。被判定为长任务的 Agent 可创建一个
   `running` 状态 Post；最终结果原地更新该 Post，不生成虚构百分比或 ETA。
4. Outbox 持久化 `root_id`、消息种类、Scope、Artifact refs、Mattermost file IDs、props、actions、
   update target 和稳定幂等键。进程重启后重试继续使用相同键和已上传的 file IDs。
5. `send_file` 不再接受文本、base64 或路径，也不执行 Mattermost I/O。它在审批后只校验同 Scope
   Artifact 并产生交付意图；Delivery worker 解析 Artifact、校验 Hash、上传并绑定文件。
6. 审批按钮使用 HMAC 签名、短时、持久一次性 token。Callback 每次重新校验 Mattermost actor、
   channel、Scope、审批资源和当前状态；未配置 HTTPS callback 或按钮不可用时保留文本命令。
7. 认证级 Mattermost 能力探测只允许 HTTPS 或本机受信 HTTP，并将 Server/Edition/文件/插件/交互
   开关快照写入审计。

## 结果与边界

Link、Research、PPT、Project 和默认对话 Agent 共用一个展示与交付契约。结构化 JSON 保留在
Envelope/Audit/Artifact，不作为默认聊天正文；文件外发回到 Policy、审批、Outbox 和 Delivery 的
统一控制链。

当前交互回调只把批准/拒绝接入真实审批状态机。Delivery 手工重试、Artifact 下载授权和返工创建新
Run 需要各自的业务状态与授权服务，在完成前不得只靠按钮 token 模拟成功。

## 修订

- 2026-08-01：第 2 条中的“转义”策略调整。`MattermostRenderer.clean()` 不再用反斜杠转义
  Markdown 语法（`\ ` * _ [ ] ( ) # ! | ~`），改为只保留安全边界清洗：控制字符剔除、
  `@`→`@​` 防 Mention 注入、`<`/`>` 实体化（Mattermost 不渲染 HTML）。动机：Mattermost
  用 marked 渲染 message 字段，agent 写的粗体/代码/链接/表格应原样渲染，而非退化成字面字符。
  URL 安全仍由 `safe_url` 在框架层兜底。
- 2026-08-02：收敛 Agent 输出契约。`AgentOutput.result` 成为唯一结构化业务结果，不再从
  Runtime output、文本 JSON 或多套别名中择一回退；缺少结构化结果的 Package Run 直接失败。
