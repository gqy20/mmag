"""
SDK @tool 定义 — 7 个内置工具从 Tool dataclass 迁移到 @tool 装饰器格式。

每个函数都是 async（SDK @tool 要求），sync handler 通过 asyncio.to_thread() 桥接。
返回格式: {"content": [{"type": "text", "text": json_string}]} (PoC 验证的协议)。
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from claude_agent_sdk import tool

from .capabilities.bindings import bind_sdk_capability
from .capabilities.catalog import (
    create_get_channel_info_capability,
    create_get_posts_capability,
    create_get_user_profile_capability,
    create_search_knowledge_capability,
    create_search_messages_capability,
)
from .capabilities.link import create_analyze_link_capability

# ============================================================
# 工具参数上限（与 builtin.py 保持同一份数字）
# ============================================================

# send_file 文件大小上限 (10MB, Mattermost 单文件限制通常 50-100MB,这里保守)
SEND_FILE_MAX_BYTES = 10 * 1024 * 1024

# 用户消息中请求文件的关键词 (触发 send_file 的硬约束)
_FILE_REQUEST_KEYWORDS = (
    "发文件", "发个文件", "导出", "下载", "存成文件", "存为文件",
    "发给我", "发个文档", "保存为", "打包", "附件",
    "send file", "export", "download", "as file", "as attachment",
)


class ToolContext:
    """Agent 与 SDK 工具之间的共享上下文

    每次消息处理前由 Agent 设置 current_post,
    send_file 等需要感知用户意图的工具从中读取。
    """

    def __init__(self):
        self.current_post: dict | None = None

    def user_requests_file(self) -> bool:
        """检查当前用户消息是否明确请求发文件"""
        if not self.current_post:
            return False
        msg = self.current_post.get("message", "").lower()
        return any(kw in msg for kw in _FILE_REQUEST_KEYWORDS)


# ============================================================
# 公共入口
# ============================================================


def create_sdk_tools(mm_client, memory, tool_context: ToolContext | None = None) -> list:
    """创建 @tool-decorated 函数，通过闭包注入 mm_client / memory / tool_context。

    返回的 list 可直接传给 create_sdk_mcp_server(tools=[...])。
    """
    if tool_context is None:
        tool_context = ToolContext()
    return [
        _make_sdk_get_posts(mm_client, memory),
        _make_sdk_search_messages(memory),
        _make_sdk_search_knowledge(memory),
        _make_sdk_get_channel_info(mm_client),
        _make_sdk_save_knowledge(memory),
        _make_sdk_get_user_profile(mm_client, memory),
        _make_sdk_analyze_link(memory),
        _make_sdk_send_file(mm_client, tool_context),
    ]


# ============================================================
# 工具工厂 — 每个返回一个 @tool-decorated async 函数
# ============================================================


def _make_sdk_get_posts(mm_client, memory):
    return bind_sdk_capability(create_get_posts_capability(mm_client, memory))


def _make_sdk_search_messages(memory):
    return bind_sdk_capability(create_search_messages_capability(memory))


def _make_sdk_search_knowledge(memory):
    return bind_sdk_capability(create_search_knowledge_capability(memory))


def _make_sdk_get_channel_info(mm_client):
    return bind_sdk_capability(create_get_channel_info_capability(mm_client))


def _make_sdk_save_knowledge(memory):
    @tool(
        "save_knowledge",
        (
            "向团队知识库中存储一条知识。"
            "用于记住从对话中学到的重要事实、决策或结论。"
            "不要存储琐碎信息，只存有长期价值的内容。"
        ),
        {"channel_id": str, "key": str, "value": str},
    )
    async def sdk_save_knowledge(args):
        result = await asyncio.to_thread(
            _save_knowledge, memory, args["channel_id"], args["key"], args["value"]
        )
        return _sdk_tool_return(result)

    return sdk_save_knowledge


def _make_sdk_get_user_profile(mm_client, memory):
    return bind_sdk_capability(create_get_user_profile_capability(mm_client, memory))


def _make_sdk_analyze_link(memory):
    return bind_sdk_capability(create_analyze_link_capability(memory))


def _make_sdk_send_file(mm_client, tool_context: ToolContext):
    @tool(
        "send_file",
        (
            "向频道发送文件附件。仅在用户明确请求发文件/导出/下载时调用。"
            "content 为文本内容 (UTF-8)，适用于 .md/.txt/.json/.csv/.html/.py 等文本格式；"
            "content 为 base64 编码时需设 content_encoding='base64'，适用于 .pptx/.xlsx/.pdf 等二进制格式。"
            "filename 决定文件类型和下载名。message 为附带的文字说明 (可选)。"
        ),
        {
            "filename": str,
            "content": str,
            "message": str,
            "content_encoding": str,
        },
    )
    async def sdk_send_file(args):
        if not tool_context.user_requests_file():
            return _sdk_tool_return(
                {"error": "用户未明确请求发送文件。只有在用户说'发文件/导出/下载'等时才可调用此工具。"}
            )

        filename = args["filename"]
        content = args["content"]
        encoding = args.get("content_encoding", "text")
        message = args.get("message", "")

        if encoding == "base64":
            try:
                data = base64.b64decode(content)
            except Exception as e:
                return _sdk_tool_return({"error": f"base64 解码失败: {e}"})
        else:
            data = content.encode("utf-8")

        if len(data) > SEND_FILE_MAX_BYTES:
            return _sdk_tool_return(
                {"error": f"文件过大 ({len(data)} bytes), 上限 {SEND_FILE_MAX_BYTES} bytes"}
            )

        post = tool_context.current_post or {}
        channel_id = post.get("channel_id", "")
        root_id = post.get("id", "")

        if not channel_id:
            return _sdk_tool_return({"error": "无频道上下文,无法发送文件"})

        file_id = await asyncio.to_thread(
            mm_client.upload_file, channel_id, filename, data
        )
        if not file_id:
            return _sdk_tool_return({"error": "文件上传失败"})

        post_id = await asyncio.to_thread(
            mm_client.send_post,
            channel_id,
            message or f"📎 {filename}",
            root_id,
            None,
            [file_id],
        )
        if not post_id:
            return _sdk_tool_return({"error": "文件已上传但消息发送失败", "file_id": file_id})

        return _sdk_tool_return(
            {"success": True, "filename": filename, "file_id": file_id, "size_bytes": len(data)}
        )

    return sdk_send_file


# ============================================================
# SDK 工具返回格式
# ============================================================


def _sdk_tool_return(result_data: Any) -> dict:
    """包装工具结果为 SDK 要求的格式。

    PoC 验证: @tool 函数必须返回 {"content": [{"type": "text", "text": json_string}]}。
    """
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result_data, ensure_ascii=False),
            }
        ]
    }


def _save_knowledge(memory, channel_id: str, key: str, value: str) -> dict:
    """保存知识并返回确认"""
    memory.add_knowledge(channel_id, key, value)
    return {"status": "ok", "key": key, "message": f"已记住: {key}"}
