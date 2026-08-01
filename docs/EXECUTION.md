# 受控执行平面

MMAG 的生产 Python/CLI 执行以平台注册的窄口 Capability 为默认方式：模型只提交业务参数，平台选择已发布的 `ExecutionProfile`、已登记脚本和固定 argv。当前 `ppt@2.1.0` 为了 Demo 可用性显式开放 `ppt.shell` 宿主机 Shell，这是已知的临时高风险例外，不代表生产安全边界。

```text
Agent allowlist
  ∩ active Skill capabilities
  ∩ Package Policy decision
  ∩ Execution Profile command
  → ProcessRunner → Artifact staging → Artifact Repository
```

任意一层缺失都在启动进程前拒绝。Agent/Skill Manifest 只能引用精确 Profile 版本并缩小权限，不能声明新的 runner、可执行文件、挂载、网络或环境继承。

## 包与注册

Profile 使用扁平目录，版本只保留在 YAML：

```text
execution-profiles/
  ppt.yml                 # metadata.version: 1.0.0

skills/slides/
  skill.yml               # execution_profiles: [ppt@2.1.0]
  scripts/ppt.cjs         # 锁定依赖的离线 bundle，只哈希和执行，不向模型披露
```

`ExecutionProfileLoader` 使用 Draft 2020-12 Schema 严格拒绝未知字段，并额外校验文件名、命令 ID、可信可执行文件、占位符和挂载边界。Registry 以 `name@version` 原子加载；Profile YAML Hash、runner、运行时镜像/依赖摘要与网络模式进入 Agent Package、Skill provenance 和 Artifact provenance。Profile 内容变化必须提升 SemVer，CI 会比较目标分支。

当前 `ppt@2.1.0` 向 PPT Agent 暴露高层 `ppt.build` 与 Demo 专用 `ppt.shell`。`ppt.build` 内部注册四个固定步骤：

- `ppt.source`：校验并保存规范化 `slides.md`；
- `ppt.render`：固定调用锁定的 PptxGenJS bundle，输出原生可编辑 `slide_deck` PPTX；
- `ppt.preview_svg`：从同一份 Markdown 与主题直接生成首页 SVG；
- `ppt.preview`：固定调用 rsvg-convert 输出 1280×720 PNG 并校验尺寸。

内部步骤不是模型 Capability。`ppt.build` 负责依次执行并只在源文件、PPTX 和预览均成功后返回完整 Bundle；PptxGenJS、主题和 Markdown 语法版本写入每个 Artifact provenance。PDF 不再是生成预览的前置条件。

`ppt.shell` 通过注册脚本启动完整 Bash，允许任意命令字符串、宿主文件和网络访问。它从独立临时目录启动，父进程环境被清空且 stdout/stderr 有上限，但这不是文件系统 Sandbox；投入生产前必须移除或重新绑定到真正隔离的 Runner。

## 隔离与生命周期

`ProcessRunner` 使用 `asyncio.create_subprocess_exec`。PPT Demo 的 `host` Runner 清空父进程环境、从本次 Run 的 `tmp` 启动，并保留 wall-clock timeout、CPU、地址空间、增量进程数、文件大小、文件描述符与 stdout/stderr 总量限制；它不隔离宿主文件系统或网络。Bubblewrap Runner 仍用于受控 Profile，但当前 PPT Profile 不依赖它。

每次调用创建独立 `runs/<run-hash>-<random>/` 工作区：

```text
input/      # 只读业务输入、同 scope 来源 Artifact
assets/     # 只读且 SHA-256 匹配的 Skill 脚本
tmp/        # 进程临时文件
staging/    # 唯一产物出口
```

成功产物经过普通文件、非符号链接、大小和 SHA-256 校验后，以临时目录加原子 rename 提交到文件型 Artifact Repository，并把 metadata 写入 SQLite。失败、取消和超时都会清理当前工作区；启动时回收超过 retention 的 Run 目录，并协调清理中断的 `.commit-*` 与无 SQLite 记录的孤儿产物。若持久记录指向缺失或不可信文件则启动失败，不静默丢失证据。Artifact ref 解析再次校验 scope、kind、路径、大小和内容 Hash。

## 审计

每次进程调用写入 `execution.process`：

- trace/run scope、actor、Capability/command 与成功、失败或取消状态；
- Profile ref/Hash、运行时摘要、runner 与网络模式；
- 输入摘要和脚本 ref/Hash，不记录完整输入；
- 固定 argv 摘要、实际可执行文件 Hash；
- return code、duration、stdout/stderr 字节和 Artifact ref/Hash/大小；
- 生效的 timeout、CPU、内存、进程、输出和 Artifact 上限；
- 稳定错误码，如 `sandbox_unavailable`、`execution_timeout`、`output_limit`。

Capability 常规日志同样只记录参数 key 和输入摘要。stdout/stderr 内容、消息正文和 Secret 不进入执行审计。

## 部署前提与失败方式

PPT Demo 主机必须安装 Node.js 与 rsvg-convert。PPTX 由锁定的 PptxGenJS bundle 生成，预览不依赖 LibreOffice。

可执行文件、资源限制或渲染器不可用时，能力返回结构化执行错误，不静默换用另一个渲染器。当前开发环境已完成真实 PPTX 与 PNG smoke。

相关配置：

```text
EXECUTION_PROFILES_PATH
EXECUTION_RUNTIME_ROOT
EXECUTION_WORKSPACE_PATH
EXECUTION_WORKSPACE_RETENTION_SECONDS
ARTIFACT_STORE_PATH
```

`execution-profiles/*.yml`、Agent/Skill Manifest、Prompt 或 Policy 中都禁止存放 Secret。当前 Profile 也不提供 Secret 注入字段；未来若确有需求，必须由平台 Secret Provider 按 Capability 和请求 Policy 显式注入，不能恢复父环境继承。

## 与 LangGraph、Deep Agents Sandbox 的边界

Process Runner 不是 LangGraph 原生组件。LangGraph 只负责图状态、
工具调度、checkpoint 与 `interrupt/resume`；真正的 Python/CLI 进程始终由执行平面负责。

Deep Agents 是构建在 LangGraph 上层的 Agent Harness，可以通过可插拔 Sandbox Backend 向 Agent
提供文件读写和 `execute`。该能力适合 Coding、数据分析和复杂媒体生成等需要临时代码的任务，
但它的通用命令入口不等价于本章的固定 Capability，也不能直接替换现有安全边界。

后续计划采用双通道模型：

```text
governed execution
  Capability → Execution Profile → 固定脚本/argv → Bubblewrap 或 OCI → Artifact

autonomous execution（尚未实现）
  专用 Agent Package → Deep Agents Runtime → thread-scoped Sandbox → Artifact
```

两个通道共同遵守 Agent/Skill allowlist、Package Policy、审批、预算、Artifact scope 和审计。自主
通道还必须满足：

- Sandbox Provider 由平台代码注册，Manifest 只能引用版本化 `SandboxProfile`；
- 默认按 thread 创建和销毁，不共享宿主目录、父环境或 Secret；
- 只通过校验后的 Artifact API 上传输入和下载输出；
- `execute` 只对显式授权的自主执行 Package 可见，不向普通业务 Agent 开放；
- MMAG Skill 脚本默认不投影到 Sandbox，只有显式声明的可编辑资源才能进入；
- Sandbox 调用和 Artifact 提交使用稳定幂等键，不依赖模型避免重复副作用；
- 不允许在本地 Bubblewrap、远程 Sandbox 或不同 Runtime 之间静默降级。

详细决策和实施前置条件见
[ADR-0007：可选 Deep Agents Runtime 与双通道 Sandbox](adr/0007-deepagents-sandbox-runtime.md)。
