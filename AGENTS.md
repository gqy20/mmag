# MMAG Repository Instructions

本文件约束在本仓库中工作的编码智能体。它不是 `agents/*/agent.yml` 的运行时 Agent 定义，也不能
代替 Agent Package、Skill、Policy、Execution Profile 或 Prompt。

## 适用范围与优先级

- 本文件适用于整个仓库；子目录中的 `AGENTS.md` 可以增加或收紧局部规则，但不能放宽这里的安全
  和权限边界。
- 用户当前请求和明确授权高于本文件中的默认工作方式。
- 发生冲突时按以下顺序判断：用户授权边界；权限、安全、Secret、数据完整性和不可逆副作用；当前
  需求正确性；公共契约与测试；最小改动和交付速度；性能、美观与未来扩展性。
- 代码是当前事实来源。Roadmap、Proposed ADR 和注释中的未来设计不得当作已实现能力。

## 项目事实

- Python 3.12+，使用 `uv` 管理 Python 依赖。
- LangGraph 是默认 Agent Runtime，负责图状态、工具调度、checkpoint 和人工审批恢复。
- Claude Agent SDK 是显式可选 Runtime，不得把 SDK 内置 Bash、文件或命令工具暴露给普通 Bot。
- Deep Agents Runtime 与自主 Sandbox 当前是 Proposed/Roadmap 能力，尚未实现。
- Agent、Skill、Policy、Model Policy、Capability 和 Execution Profile 均采用显式注册、严格 Schema、
  版本和 provenance。
- Mattermost 是当前主要入口与交付平台；control plane、Outbox、审批和 Artifact 有独立业务状态。
- 当前是模块化单体。没有容量、隔离或部署证据时，不拆分微服务。

## 必读文档路由

只读取当前任务需要的文档，不要无目的遍历全部文档：

- 项目入口、目录和安全基线：`README.md`
- 当前完成状态和后续工作：`docs/ROADMAP.md`
- Agent Manifest、Provider 和运行契约：`docs/AGENT_PACKAGES.md`
- Skill、资源披露和脚本规则：`docs/SKILL_PACKAGES.md`
- Python/CLI、Bubblewrap、工作区和 Artifact：`docs/EXECUTION.md`
- 部署和运维：`docs/OPERATIONS.md`
- 已知缺口：`docs/TECH_DEBT.md`
- 架构理由和不可逆决策：`docs/adr/`

修改架构前必须读取相关 ADR。ADR 状态为 Proposed 时，只能作为规划依据，不能假设代码已经支持。

## 范围与效率控制

默认目标是用最小、正确、可验证的改动完成用户明确要求，而不是追求理论上的最优架构。

### 按请求动词确定授权范围

- “分析、检查、解释、评审、先说明”：只读分析，不修改代码或文档。
- “规划、设计、给方案”：输出方案，不提前实现。
- “修改文档”：只修改相关文档，不顺手修改代码。
- “诊断”：确定原因和证据；除非请求包含修复，否则不实现修复。
- “修复”：修复已确认的问题，不顺手重构相邻模块。
- “实现、构建”：实现明确需求和完成需求必需的支撑改动。
- “重构”：可以调整指定边界内的结构，但不得扩展成未请求的功能。
- 用户要求“不运行”时，不启动项目、测试、脚本、CLI、服务、外部探测或 smoke。

### 最小充分改动

- MUST 优先复用当前架构、契约、类型和依赖。
- MUST NOT 因代码“不够优雅”而扩大当前任务。
- MUST NOT 顺手修复与当前需求无关的问题。
- MUST NOT 为假设中的未来需求增加抽象、接口、兼容层、配置或 Provider。
- MUST NOT 同时实现多个备选方案；除非用户要求比较，只选择最符合现有架构的一种。
- MUST NOT 新增依赖，除非现有能力无法正确完成需求且新增依赖属于任务必要范围。
- SHOULD 保持修改文件数量、公共 API 和数据迁移范围尽可能小。
- SHOULD 在局部修复足够时避免跨模块重构。
- 单文件未超过约 800 行且职责仍稳定时，不为形式上的分层拆成碎片模块。

### 扩展门禁

只有满足以下至少一项时才扩大任务范围：

1. 不扩大就无法满足用户明确需求；
2. 不扩大将导致代码无法运行、相关测试无法通过或数据损坏；
3. 当前改动将产生明确的权限、安全、幂等或不可逆副作用问题；
4. 用户明确批准额外范围。

性能微调、命名偏好、减少少量重复、风格统一和未来扩展性不是自动扩展范围的理由。发现范围外问题时，
最多在交付中简短记录影响与建议；不要主动实现、创建 Issue、添加 TODO 或修改 Roadmap。

### 调研与优化门禁

- 从用户指定文件、入口、直接依赖和相关测试开始。
- 获得足够证据回答或修改后停止探索；只有发现矛盾、缺失证据或跨模块影响时才扩大阅读范围。
- 不为简单任务制作长计划、多套实现、无必要架构图或仓库级审计。
- 不重复读取已确认且未发生变化的内容。
- 外部资料只在用户要求、信息具有时效性或本地代码不足时查询；技术问题优先官方源码和官方文档。
- 主动优化前必须确认：直接影响验收标准；有代码、测试、日志或风险证据；当前任务是正确修改位置；
  收益明显高于成本和回归风险。任一条件不满足就不实施。

### 停止条件

同时满足以下条件后必须交付，不得继续“顺便优化”：

- 用户明确要求已经完成；
- 当前验收标准已经满足；
- 直接相关验证已通过，或已说明未验证原因；
- 当前改动没有引入已知正确性、安全或数据完整性问题；
- 剩余事项属于独立需求或低优先级改善。

“还可以更优雅”“以后可能需要”“顺便统一一下”不是继续工作的理由。

## 压缩执行流程

开始前在内部确定 Deliverable、Scope、Non-goals、Verification 和 Stop 条件；除非存在歧义或阻塞，
不需要把完整检查过程输出给用户。正确性和安全边界不得因追求快速而降级，但必须通过批处理、
并行和停止条件减少无价值的工具往返。

### 一次批量发现

- 首轮应在一个工具往返中完成必需的 `git status`、`rg` 定位、关键代码片段和相关测试入口读取。
- 多个互不依赖的只读检查必须在同一工具轮次并行执行；不要为每个文件单独启动命令。
- 先用 `rg` 定位精确符号和行号，再读取有界片段；除非文件很短或必须建立整体上下文，不输出整个文件。
- 已确认且未修改的内容不得重复读取；后续只读取新发现的直接依赖或发生变化的 hunk。
- 第三方 SDK 签名、版本或运行时能力是实现前提时，必须在首轮发现中一次验证，不要到实现后再补查。

### 集中修改

- 契约和修改点明确后，优先使用一次聚焦的 `apply_patch` 完成同一逻辑批次，避免无新证据的
  “改一点—重读—再改一点”循环。
- 格式化或批量机械变换可使用项目工具；手工文本修改使用 `apply_patch`。
- 工作区存在大批既有改动时，先用 `git diff --name-only` / `--stat` 缩小范围，只查看本次相关路径和 hunk；
  不反复输出整个工作区 Diff。
- 不因文件接近大小阈值就拆分；仅在超过约 800 行且职责已经分化时处理拆分。

### 一次并行验证

- 先确定最小相关验证集；Ruff、Mypy、定向 Pytest 和 `git diff --check` 互不依赖时，必须在同一工具轮次
  并行执行，不得为每项检查支付一次串行往返。
- 验证失败后只修改失败直接相关内容，并只重跑失败项和必要的回归项；不从全部检查重新开始。
- 用户要求先完成整体或明确不跑全量测试时，不得通过扩大测试集来延长交付；只保留直接契约和高风险边界验证。
- 文档修改只做精确内容复查、链接/格式检查和 Diff 检查，不运行应用测试。

### 工具往返预算

- 边界清晰的局部文档任务默认为 3 轮：批量发现 → 一次修改 → 一次复查与交付。
- 边界清晰的局部代码任务默认不超过 6 轮：批量发现 → 集中修改 → 最小测试补充 → 并行验证 → 必要修复 → 交付；
  只在经授权时提交。
- 超出预算前必须有新证据：验证失败、发现直接跨模块影响、安全/数据完整性风险或外部阻塞。仅仅“再确认一次”
  不构成超预算理由。
- 除非用户、系统或任务复杂度要求，边界清晰的局部任务不创建详细计划，不在每个微小步骤后更新计划。
- `git status` 默认只在开始确认工作区、交付前确认范围和经授权提交后确认结果时执行，不在每次 Patch 后重复运行。

### 标准顺序

1. 批量了解工作区、定位入口、直接调用方、契约和相关测试。
2. 读取最小充分代码，确认事实后集中编辑。
3. 一次并行运行最小相关验证，根据失败证据决定是否扩大。
4. 达到停止条件后立即交付；只报告实际修改、验证结果、未验证项和重要剩余风险。
5. Commit、push、PR 和远程操作仍只在用户明确授权时执行，不为了减少往返而推定授权。

## 架构不变量

- MUST 通过 `AgentRuntime`、`RunRequest` 和 `AgentResult` 契约接入新 Runtime。
- MUST 保持 `CapabilitySpec` 为业务能力的单一事实来源。
- MUST 让 LangGraph、Claude SDK 和 MCP 适配层复用 `CapabilityExecutor`，不得复制业务授权实现。
- MUST 在副作用前完成 Agent/Skill allowlist、Package Policy、actor、scope、permission、动态资源和
  审批检查。
- MUST 保持 Agent/Skill Manifest 只能引用和缩小平台已注册权限，不能创建权限、runner、挂载、
  endpoint 或 Secret。
- MUST 使用严格 Envelope 和 Artifact ref 进行 Agent handoff，不依赖自由文本猜测下游输入。
- MUST 保持展示层与 Agent 语义结果分离；Agent Prompt 不得直接生成 Mattermost `props`、action
  callback 或平台专用协议。
- MUST NOT 恢复旧 Agent、旧 Tool、全局 Prompt、手写 Agent loop 或兼容转发层。
- MUST NOT 在可能产生副作用后自动跨 Runtime/Provider 重试。

## LangGraph 与审批

- `interrupt()` 前只能执行确定性、无副作用的授权和状态准备。
- 审批与真实 Capability 执行必须分离。
- 恢复必须使用原始稳定 `thread_id` 和 `Command(resume=...)`。
- interrupt payload 和 checkpoint state 必须可安全序列化。
- 恢复会重放节点逻辑；外部写入必须使用稳定 execution/idempotency key，不能假设 checkpoint 提供
  exactly-once 副作用。
- approve、edit、reject 必须保留原 actor、scope、Package/Skill snapshot 和审计关系。
- 工具和 Artifact 状态优先使用结构化结果，不从模型自然语言中反解析控制状态。

## Capability 与 MCP

新增或修改 Capability 时：

- 定义唯一 `CapabilitySpec`、严格 JSON Schema、READ/WRITE effect、permission、timeout 和 source policy。
- 通过 `CapabilityExecutor` 统一验证、授权、执行和错误语义。
- 从可信请求 Context 校验 actor、scope 和资源参数；模型参数不能成为可信授权事实。
- WRITE 或外部副作用必须评估审批、幂等、重试和审计。
- 为 LangGraph、SDK、MCP 只添加薄适配器，不复制 handler。
- 测试成功、非法输入、未知能力、拒绝、越权、审批和超时。

MCP 默认不连接；工具必须进入精确 allowlist。可见性不等于执行授权。stdio MCP 只继承最小环境，
不得继承 Mattermost、模型或其他无关 Secret。

## Agent Package

新增或修改 Agent Package 时：

- 使用 `agents/<name>/agent.yml` 扁平目录，目录名与 Manifest name 一致。
- 提供真实 Prompt、输入/输出/Artifact Schema、Policy、Model Policy 和 eval。
- 绑定精确版本的 Skill 与 Execution Profile。
- Capability 必须来自已注册 Catalog；不得通过 Prompt 或 Manifest 自授权。
- YAML 只能选择代码注册的稳定 Provider，不能填写 Python import 路径。
- Package 内容变化必须按项目版本规则提升 SemVer，并更新 provenance/Hash 相关断言。
- 不注册没有真实执行器和契约的占位 Agent。

## Skill Package

新增或修改 Skill 时：

- 必须包含 `skill.yml`、`SKILL.md` 和严格输入/输出 Schema。
- required/optional Capability 必须是当前 Agent 已允许能力的子集。
- Execution Profile 必须同时被 Agent Package 允许。
- instruction 只描述工作方法；reference/template 按预算渐进披露。
- `resources.scripts` 不进入模型上下文，不作为普通可披露资源。
- Skill 脚本只能由受信 `ScriptExecutor` 按 Hash 和固定 Execution Profile 执行。
- Skill 不能通过路径、Prompt、脚本内容或 Manifest 自行启动进程。
- Skill 内容变化必须提升版本并更新快照、测试和相关 Package 引用。

## Python、CLI 与 Sandbox

- MUST NOT 使用 `shell=True`、`os.system`、任意命令字符串、动态 `eval/exec`。
- MUST NOT 向普通 Agent 注册 `shell.exec`、`python.exec` 或 `python.eval`。
- MUST 使用 `asyncio.create_subprocess_exec` 和固定 argv。
- MUST 通过已注册、版本化的 Execution Profile 执行 Python/CLI。
- MUST 校验脚本、输入、来源 Artifact、输出、路径、scope、kind 和 Hash。
- MUST 默认断网、清空父进程环境、限制可写目录和资源。
- Sandbox、runtime 或依赖不可用时必须失败关闭，禁止回退到宿主机执行。
- Secret 不得写入 Agent/Skill Manifest、Execution Profile、Prompt、日志或 Artifact。
- Deep Agents 自主执行入口在 ADR-0007 的门禁完成前不得启用；当前 `resources.scripts` 也不得投影给
  模型自由修改和执行。

涉及执行面的修改至少考虑：命令注入、路径穿越、符号链接、环境泄漏、未授权网络、资源超限、
跨 scope Artifact、重复恢复和缺失 Sandbox。

## Mattermost 与外部系统

- 用户、频道、团队和文件目标必须来自可信请求 Context，并在副作用前重新校验。
- 文件生成只产出 Artifact；上传、发帖、重试和幂等由 Delivery/Outbox 管理。
- 默认测试不得访问真实 Mattermost、真实 LLM、真实 MCP 或公网。
- 不在明文 HTTP 连接中使用 Bot Token 做管理级探测。
- URL 抓取必须保持代理禁用、逐跳重定向检查、DNS/IP SSRF 校验和响应大小限制。
- 日志不得记录 Token、Secret、完整敏感正文或未脱敏工具参数。

## 验证预算

用户未禁止运行时，按改动范围选择最小充分验证：

- 格式和 lint：`uv run ruff check <changed paths>`
- 相关测试：`uv run pytest <relevant tests>`
- 类型检查：公共类型或跨模块修改时运行 `uv run mypy src/mmag`
- 完整门禁：只有核心 Runtime、权限、Schema、构建或跨模块修改时运行 `make verify`

规则：

- 文档修改只做静态格式、链接和 diff 检查，不运行应用测试。
- 不启动 Bot，不调用真实 LLM/Mattermost/MCP/公网，不运行真实 Sandbox 或 LibreOffice smoke，除非用户
  明确授权。
- 外部测试必须保留 explicit marker，不得进入默认离线测试集合。
- 不为提高无关覆盖率修改额外代码。
- 安全、权限、幂等和数据完整性边界需要相应负向测试；普通局部逻辑不无限追加假设性边缘测试。
- 不自动运行 `make clean`。

## 文档规则

- 文档必须区分“当前已实现”“Proposed”“Roadmap”“已知技术债”。
- README 只描述用户当前可以使用的能力。
- Agent 变化同步 `docs/AGENT_PACKAGES.md`；Skill 变化同步 `docs/SKILL_PACKAGES.md`；执行与 Sandbox
  变化同步 `docs/EXECUTION.md`。
- 未完成工作写入 `docs/ROADMAP.md` 或 `docs/TECH_DEBT.md`，但不要为当前范围外发现主动扩写 Roadmap。
- 架构决策变化新增或修订 ADR，并说明状态、背景、决策、后果和边界。
- 不因局部实现自动把整个 Roadmap 阶段标记完成。

## Git 与工作区安全

- 用户已有修改属于用户；保留无关和重叠改动，不得擅自覆盖、格式化或回滚。
- 禁止使用 `git reset --hard`、`git checkout --` 或其他会丢失工作区内容的命令。
- 未经用户明确要求，不 commit、push、创建 PR、修改远程状态或重写历史。
- 删除、移动、批量改写或生成大量文件前确认目标和范围。
- 不把临时文件、运行日志、Secret、真实 Token、Artifact 二进制或本地数据库意外提交到仓库。

## 完成标准

任务只有在以下条件满足时才算完成：

- 实现与用户请求、当前代码和 Accepted ADR 一致；
- 没有扩大 Agent、Skill、Capability、MCP、Sandbox 或外部资源权限；
- 输入、输出、错误、副作用、幂等和恢复边界与改动规模相匹配；
- 最小相关验证已通过，或明确说明未运行及原因；
- 相关文档已同步，且没有把规划写成事实；
- 用户已有修改未被破坏；
- 最终回复简洁列出结果、修改文件、验证情况和真正重要的剩余风险。
