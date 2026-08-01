# ADR-0007：可选 Deep Agents Runtime 与双通道 Sandbox

- 状态：Proposed
- 日期：2026-08-01

## 背景

当前默认 Runtime 是 LangGraph，受控执行平面由 MMAG 自己实现。LangGraph 负责状态、调度、
checkpoint 和人工审批；`ProcessRunner` 使用 Bubblewrap 执行已注册 Skill 中经过 Hash 校验的
脚本和固定 argv。Bubblewrap 不是 LangGraph 原生 Sandbox，当前通道也有意不向模型暴露通用
Shell、动态 Python、宿主机路径或父进程环境。

Presentation、PDF 转换等确定性生产任务适合该模式。未来的 Coding、数据分析、复杂媒体生成等
任务可能需要 Agent 在隔离文件系统中创建、修改和运行临时代码。Deep Agents 在 LangGraph 上层
提供文件系统、Skills、子智能体与可插拔 Sandbox Backend，但直接替换现有 Runtime 会扩大权限面，
并绕开 MMAG 已有的 Agent Package、Capability、Policy、Artifact 和审批契约。

## 决策

1. 保留 LangGraph 作为默认 Runtime，保留当前受控执行通道用于可信、版本化 Skill 脚本和固定
   Capability；不因引入 Deep Agents 而向普通业务 Agent 暴露 `execute(command)`。
2. Deep Agents 只作为显式可选的自主执行 Runtime，通过新的 `DeepAgentRuntimeAdapter` 接入统一
   `RunRequest -> AgentResult` 契约，不替换 Agent Router、Model Gateway 或 control plane。
3. 执行平面形成两个权限不相交的通道：
   - `governed`：固定 Capability、固定 argv、可信脚本，可由本地 Bubblewrap 或未来 OCI Backend
     执行；
   - `autonomous`：允许 Agent 在一次性 Sandbox 中读写文件和运行临时代码，只对专用 Package
     开放。
4. Deep Agents Tool 必须由现有 `CapabilitySpec` 投影，授权、审批、预算和审计仍进入
   `CapabilityExecutor`；Sandbox 的 `execute`、上传、下载和销毁也必须注册为受 Policy 控制的
   Capability，不能成为隐式旁路。
5. MMAG Skill 是源事实。投影到 Deep Agents 时默认只披露 instruction、允许的 reference/template；
   `resources.scripts` 继续隐藏。只有 Manifest 显式标记为 sandbox 可编辑的资源才能复制进自主
   Sandbox。
6. 自主 Sandbox 默认按 LangGraph thread 隔离和回收。模型/API/Mattermost/MCP Secret 留在宿主
   控制面；Sandbox 只接收经过 scope、kind、Hash 校验的输入 Artifact，只能通过下载、校验和
   `ArtifactRepository.commit` 输出结果。
7. Sandbox Provider 只能由平台注册表选择，Manifest 只能引用版本化 `SandboxProfile`，不能填写
   Python import、endpoint、credential、宿主挂载或放宽网络。候选 Backend 包括 Deep Agents
   兼容的 LangSmith、Daytona、Modal、Runloop 以及平台自定义实现。
8. 不在 Runtime 之间自动回退或重试。自主执行使用稳定 execution key，Sandbox 创建、命令执行、
   Artifact 提交和销毁均需幂等审计；崩溃恢复不能重复产生外部副作用。

## 结果与边界

- 确定性 PPT/PDF 等任务继续使用现有受控执行平面，保持最小权限、固定输入输出和可重复性。
- 需要自主编程的专业 Agent 可以获得 Deep Agents 的文件系统和执行循环，但自由度被限制在独立
  Sandbox 内，不能影响 MMAG 宿主或其他 thread。
- Deep Agents 集成是新增 Provider、Runtime Adapter 和投影层，不是第二套 Agent/Skill/Policy
  注册系统。
- 在 `SandboxProfile`、Artifact 边界、审批、幂等、资源配额和目标环境验收完成前，本 ADR 不视为
  Accepted，也不得为现有 Package 开放自主 Shell。

