"""Governed Artifact delivery intent; Mattermost I/O belongs to Delivery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import CapabilityEffect, CapabilitySpec, SourcePolicy
from .context import CapabilityContext, get_capability_context

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..execution import ArtifactRepository

_FILE_REQUEST_KEYWORDS = (
    "发文件",
    "导出",
    "下载",
    "发给我",
    "附件",
    "send file",
    "export",
    "download",
    "attachment",
)
_MISSING_INTENT = "用户未明确请求交付文件。"


def _user_requests_file(context: CapabilityContext | None) -> bool:
    if context is None:
        return False
    # A presentation is not useful as a text-only result. The PPT Package explicitly
    # grants ppt.build, so successful deck generation is itself a delivery intent.
    if "ppt.build" in context.allowed_capabilities:
        return True
    message = context.message.lower()
    return any(keyword in message for keyword in _FILE_REQUEST_KEYWORDS)


def create_send_file_capability(
    artifacts: ArtifactRepository | None = None,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
) -> CapabilitySpec:
    """Create an approval-gated delivery intent for an existing Artifact."""

    async def send_file(artifact_ref: str, message: str = "") -> dict:
        context = context_provider()
        if not _user_requests_file(context):
            return {"error": _MISSING_INTENT}
        if context is None or not context.scope:
            return {"error": "缺少可信 Scope，无法交付 Artifact。"}
        if artifacts is None:
            return {"error": "Artifact Repository 未配置。"}
        try:
            stored, _ = artifacts.resolve(artifact_ref, scope_id=context.scope)
        except (
            KeyError,
            PermissionError,
            RuntimeError,
            OSError,
            ValueError,
        ) as error:
            return {"error": f"Artifact 不可交付：{error}"}
        return {
            "success": True,
            "deliveries": [
                {
                    "artifact_ref": stored.ref,
                    "filename": stored.filename,
                    "media_type": stored.media_type,
                    "message": message,
                }
            ],
        }

    return CapabilitySpec(
        name="send_file",
        description=(
            "请求将一个已生成、同 Scope 的 Artifact 作为 Mattermost 附件交付。"
            "PPT Agent 生成演示文稿后默认为 PPTX 创建交付意图；其他 Agent 仍需用户明确请求。"
            "本能力只创建交付意图，不读取任意路径、不接收文件内容，也不直接上传或发帖。"
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "artifact_ref": {
                    "type": "string",
                    "pattern": "^artifact://[a-f0-9]{32}$",
                },
                "message": {"type": "string", "maxLength": 1000, "default": ""},
            },
            "required": ["artifact_ref"],
        },
        handler=send_file,
        effect=CapabilityEffect.WRITE,
        permission="mattermost:file:write",
        timeout_seconds=10,
        source_policy=SourcePolicy.NONE,
    )
