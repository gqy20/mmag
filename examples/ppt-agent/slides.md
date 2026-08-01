---
title: MMAG Presentation Agent
audience: 产品与技术管理层
objective: 确认 Markdown 驱动、原生可编辑的 PPT 生成路线
narrative: 从内容语义出发，经受控编译与执行，交付可编辑、可预览、可审计的演示制品
theme_ref: corp@1.0.0
---

<!-- layout: cover -->
# 让 Agent 生成真正可编辑的演示文稿

> Markdown 负责表达，PptxGenJS 负责把设计落实为 PowerPoint 原生对象

<!-- notes: 本页说明这不是网页截图方案，而是面向企业交付的原生 PPTX 管线。 -->

---

<!-- layout: statement -->
# 100%

> 当前示例中的文字、卡片、线条与流程节点均可在 PowerPoint 中直接编辑

- 内容源可回放
- 主题版本可追踪
- 输出经过统一 Capability 治理

<!-- sources: slides@2.2.0, ppt@2.1.0 -->

---

<!-- layout: architecture -->
# 四层职责分离，让生成质量与安全边界同时成立

## Agent
- 理解受众与目标
- 规划叙事和交付

## Skill
- 生成 slides.md
- 选择注册主题

## Capability
- 执行 ppt.build
- 返回 Artifact refs

## Policy
- 校验身份与 Scope
- 审批文件外发

---

<!-- layout: comparison -->
# 新链路同时改善视觉表现、编辑体验和治理能力

## 旧方案
- 固定标题与项目符号
- 版式表达能力有限
- 预览依赖 PDF 转换

## 新方案
- Markdown 语义布局
- 原生文本与 Shape
- 一次构建完整 Bundle

---

<!-- layout: timeline -->
# 一次构建经过五个确定性阶段

## 01 · Compose
- 生成受控 Markdown

## 02 · Parse
- 转换为安全 AST

## 03 · Layout
- 应用主题和网格

## 04 · Render
- 写入原生 PPTX

## 05 · Deliver
- 直接图片预览与审批交付

---

<!-- layout: split -->
# 模型专注判断，渲染器专注确定性

## 模型可以控制
- 页面叙事
- 注册布局
- 内容层级
- 引用与备注

## 模型不能控制
- JavaScript 与 Shell
- 任意 CSS 和路径
- 网络资源
- 执行参数

---

<!-- layout: end -->
# 先把“可编辑演示”做成可靠产品能力

> 下一步：用真实业务材料验证模板密度、中文排版与 Mattermost 交付体验
