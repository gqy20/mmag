# 数字员工与 Skill 清单

> 更新时间：2026-08-01

当前实现对应“企业 AI Native 协同架构”示意图中的协同中枢和四类数字员工。所有数字员工通过
`agents/*/agent.yml` 自动注册，LangGraph 是默认模型执行框架；Skill 通过 Agent 白名单选择，
Capability 调用再由当前 Package Policy 动态裁决。

## 当前数字员工

| Agent | Skill | 路由职责 | 当前可执行能力 | 结构化结果 |
|---|---|---|---|---|
| `mmchat@1.1.0` | `web-research@1.0.0` | 默认群聊/私聊协同入口 | 对话上下文、知识、链接、审批文件与受控 MCP | 文本答复 |
| `link@1.2.0` | `link-read@1.0.0` | 单 URL 解析 | 确定性 `analyze_link`，无模型循环 | `link_analysis` |
| `report@1.0.0` | `report@1.0.0` | 研报、行业/竞品研究 | 当前频道知识/消息、公开链接、按需模板 | 证据账本式 research report |
| `ppt@1.1.0` | `slides@1.1.0` | PPT、幻灯片、路演结构与文件生成 | 当前频道知识、按需模板、受控 PPTX/PDF 渲染、审批后文件交付 | slide deck 结构与 Artifact ref |
| `project@1.0.0` | `project@1.0.0` | 项目计划、状态和任务拆解 | 当前频道消息/知识、审批后写入共享知识 | project brief |

`link` 保留确定性 Provider，因为单 URL 提取不需要增加模型成本和不确定性。它的 Skill 提供输入、
输出与版本 provenance 契约，不把一个原子动作强行改造成多轮 Agent。

## 权限交集

每次执行的有效能力始终是：

```text
Agent Manifest allowlist
∩ Active Skill required/available optional
∩ Current Package Policy
∩ Execution Profile command
```

- `report` 是只读 Agent；消息和知识搜索必须显式携带当前 conversation id；
- `ppt` 的 `ppt.render` / `ppt.export_pdf` 只能进入固定 argv、默认断网的 `ppt@1.0.0` Profile；只有用户明确要求对外交付时 `send_file` 才进入 LangGraph 审批；
- `project` 的读取被限制在当前频道；`save_knowledge` 会改变共享记忆，因此需要审批；
- `link` 只能执行受 SSRF、重定向和缓存边界保护的 `analyze_link`；
- 未知 Capability、越出当前 Skill 的 Capability、资源参数不匹配或 Policy 未命中均默认拒绝。

## 渐进式披露

`report`、`slides` 和 `project` 各自只在激活后注入 `SKILL.md` 和资源目录。模板和参考资料只有在
模型调用 `load_skill_resource(ref)` 后才进入上下文，并受单资源、累计字节、资源数量和估算 token
预算限制。`scripts/` 不可读取；`slides/scripts/ppt.py` 只能由 `ScriptExecutor` 校验 Hash 后在绑定的
Execution Profile 中运行。

## 当前交付边界

这批 Package 已经提供真实路由、Prompt、Schema、Skill、Policy、预算、eval 和运行 provenance，
但没有伪装尚不存在的基础设施：

- `ppt` 已可通过受控执行平面提交 PPTX/PDF Artifact；缺少 Bubblewrap namespace 权限或 LibreOffice
  时按设计失败关闭，不降级到宿主机执行；
- `project` 当前生成计划/状态并可审批写入知识库，不声称已在 Jira、Linear 等系统创建任务；
- Agent Manifest 声明允许产生的 Artifact kind，但默认消息链尚未把结构化结果原子写入 Artifact
  Repository；
- `report → ppt` 尚未强制只通过版本化 Artifact ref 交接。

## 下一步

1. 将成功的 `report` 结果原子持久化为 `research_report@1.0.0` Artifact；
2. 让 `ppt` 只消费通过 Schema、scope 和版本校验的 Artifact ref；
3. 为 PPTX/PDF 增加封面/关键页 PNG 预览，并接入 Mattermost Presenter；
4. 将 Artifact ref 通过审批、Outbox 与文件上传交付，不让模型处理宿主路径；
5. 通过 MCP/企业 API 增加任务系统 Capability，让 `project` 在审批后创建或更新真实任务；
6. 补齐接受、驳回、返工和质量反馈，使 Task、AgentRun、Artifact、Delivery、Feedback 形成闭环。
