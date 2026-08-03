# 演示文稿

1. 明确受众、待做决定、证据边界和一句话叙事主线。
2. 需要生成文件时加载 `deck.md`，只能使用其中支持的布局和 Markdown 语法。
3. 按“背景 → 张力 → 证据 → 选项 → 建议 → 下一步行动”组织叙事。证据不足以支持该结构时，
   应明确暴露缺口，而不是强行得出结论。
4. 每页只表达一个结论式标题，支撑要点保持简洁。
5. 将补充信息写入 `notes`，将证据标识写入 `sources`，不得编造来源。
6. 只能选择已注册的 `theme_ref`，默认使用 `corp@1.0.0`。
7. 完整 Markdown 源文件准备好后，只调用一次 `ppt.build`。它负责规范化、可编辑 PPTX 渲染、
   PDF 导出、预览生成、校验和 Artifact 存储。
8. 只有同时返回 `source_ref`、`pptx_ref` 和 `preview_refs` 才算构建成功。不得伪造 ref，也不得
   换用其他渲染器重试。
9. 仅在 `ppt.build` 无法表达必要步骤时使用受治理的 `/workspace` 和 `execute`；只能通过
   `workspace.commit` 提交当前 Execution Profile 声明的固定文件名。
10. `ppt.build` 成功后，使用 `pptx_ref` 调用一次 `send_file`。交付仍需批准；只有用户明确要求时
    才发送源文件或预览文件。

不得生成 JavaScript、Python、CSS、Shell 命令、可执行文件路径、宿主机路径、任意 HTML/Vue、
远程图片 URL 或 base64。模型只控制内容和已注册布局名称；坐标、字体、主题 Token 和可执行参数由
受信渲染器控制。
