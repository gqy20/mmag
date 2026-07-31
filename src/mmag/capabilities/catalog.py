"""Built-in capability specifications."""

from __future__ import annotations

import asyncio

from ..client import channel_type_label
from .base import CapabilityEffect, CapabilitySpec, SourcePolicy


def create_get_channel_info_capability(mm_client) -> CapabilitySpec:
    """Create the canonical channel-info capability."""

    async def get_channel_info(channel_id: str) -> dict:
        channel = await asyncio.to_thread(mm_client.get_channel, channel_id)
        return {
            "id": channel.get("id", ""),
            "name": channel.get("name", ""),
            "display_name": channel.get("display_name", ""),
            "type": channel.get("type", ""),
            "type_label": channel_type_label(channel.get("type", "")),
        }

    return CapabilitySpec(
        name="get_channel_info",
        description=(
            "获取频道的详细信息，包括名称、类型、成员数等。用于了解当前所在频道的基本信息。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "频道 ID",
                },
            },
            "required": ["channel_id"],
        },
        handler=get_channel_info,
        effect=CapabilityEffect.READ,
        permission="mattermost:channel:read",
        timeout_seconds=10,
        source_policy=SourcePolicy.NONE,
    )
