"""Built-in capability specifications."""

from __future__ import annotations

import asyncio

from ..client import channel_type_label
from .base import CapabilityEffect, CapabilitySpec, SourcePolicy

SEARCH_KNOWLEDGE_DEFAULT_LIMIT = 5
SEARCH_KNOWLEDGE_MAX_LIMIT = 10


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


def create_search_knowledge_capability(memory) -> CapabilitySpec:
    """Create the canonical team-knowledge search capability."""

    async def search_knowledge(
        channel_id: str,
        query: str,
        limit: int = SEARCH_KNOWLEDGE_DEFAULT_LIMIT,
    ) -> dict:
        results = await asyncio.to_thread(
            memory.get_relevant_knowledge,
            channel_id,
            query,
            min(limit, SEARCH_KNOWLEDGE_MAX_LIMIT),
        )
        if not results:
            return {"count": 0, "items": [], "note": "未找到相关知识"}

        return {
            "count": len(results),
            "items": [
                {
                    "key": result["key"],
                    "value": result["value"],
                    "confidence": result.get("_score", result.get("confidence", 0)),
                }
                for result in results
            ],
        }

    return CapabilitySpec(
        name="search_knowledge",
        description="搜索团队知识库中的信息。用于查找之前记录的决策、流程、约定等知识。",
        input_schema={
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "频道 ID（在哪个频道的知识库中搜索）",
                },
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "返回结果数量上限 "
                        f"(默认 {SEARCH_KNOWLEDGE_DEFAULT_LIMIT}, 最大 {SEARCH_KNOWLEDGE_MAX_LIMIT})"
                    ),
                    "default": SEARCH_KNOWLEDGE_DEFAULT_LIMIT,
                },
            },
            "required": ["channel_id", "query"],
        },
        handler=search_knowledge,
        effect=CapabilityEffect.READ,
        permission="memory:knowledge:read",
        timeout_seconds=10,
        source_policy=SourcePolicy.NONE,
    )
