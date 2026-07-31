"""Mattermost file-delivery capability."""

from __future__ import annotations

import asyncio
import base64
import binascii
from typing import TYPE_CHECKING

from .base import CapabilityEffect, CapabilitySpec, SourcePolicy
from .context import CapabilityContext, get_capability_context

if TYPE_CHECKING:
    from collections.abc import Callable

SEND_FILE_MAX_BYTES = 10 * 1024 * 1024

_FILE_REQUEST_KEYWORDS = (
    "发文件",
    "发个文件",
    "导出",
    "下载",
    "存成文件",
    "存为文件",
    "发给我",
    "发个文档",
    "保存为",
    "打包",
    "附件",
    "send file",
    "export",
    "download",
    "as file",
    "as attachment",
)

_MISSING_INTENT = "用户未明确请求发送文件。只有在用户说'发文件/导出/下载'等时才可调用此工具。"


def _user_requests_file(context: CapabilityContext | None) -> bool:
    if context is None:
        return False
    message = context.message.lower()
    return any(keyword in message for keyword in _FILE_REQUEST_KEYWORDS)


def create_send_file_capability(
    mm_client,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
) -> CapabilitySpec:
    """Create the governed file-delivery capability.

    ``context_provider`` lets a persistent SDK transport expose its currently
    serialized request while direct and legacy calls use the task-local
    ContextVar provider.
    """

    async def send_file(
        filename: str,
        content: str,
        message: str = "",
        content_encoding: str = "text",
    ) -> dict:
        context = context_provider()
        if not _user_requests_file(context):
            return {"error": _MISSING_INTENT}

        if content_encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError, TypeError) as error:
                return {"error": f"base64 解码失败: {error}"}
        else:
            data = content.encode("utf-8")

        if len(data) > SEND_FILE_MAX_BYTES:
            return {"error": (f"文件过大 ({len(data)} bytes), 上限 {SEND_FILE_MAX_BYTES} bytes")}

        assert context is not None
        if not context.conversation_id:
            return {"error": "无频道上下文,无法发送文件"}

        file_id = await asyncio.to_thread(
            mm_client.upload_file,
            context.conversation_id,
            filename,
            data,
        )
        if not file_id:
            return {"error": "文件上传失败"}

        post_id = await asyncio.to_thread(
            mm_client.send_post,
            context.conversation_id,
            message or f"📎 {filename}",
            context.message_id,
            None,
            [file_id],
        )
        if not post_id:
            return {"error": "文件已上传但消息发送失败", "file_id": file_id}

        return {
            "success": True,
            "filename": filename,
            "file_id": file_id,
            "size_bytes": len(data),
        }

    return CapabilitySpec(
        name="send_file",
        description=(
            "向频道发送文件附件。仅在用户明确请求发文件/导出/下载时调用。"
            "content 为文本内容 (UTF-8)，适用于 .md/.txt/.json/.csv/.html/.py 等文本格式；"
            "content 为 base64 编码时需设 content_encoding='base64'，"
            "适用于 .pptx/.xlsx/.pdf 等二进制格式。"
            "filename 决定文件类型和下载名。message 为附带的文字说明 (可选)。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "文件名（含扩展名）"},
                "content": {"type": "string", "description": "文本或 base64 文件内容"},
                "message": {"type": "string", "description": "附件说明", "default": ""},
                "content_encoding": {
                    "type": "string",
                    "description": "content 的编码：text 或 base64",
                    "default": "text",
                },
            },
            "required": ["filename", "content"],
        },
        handler=send_file,
        effect=CapabilityEffect.WRITE,
        permission="mattermost:file:write",
        timeout_seconds=60,
        source_policy=SourcePolicy.NONE,
    )
