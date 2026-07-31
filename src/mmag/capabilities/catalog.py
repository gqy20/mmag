"""Built-in capability specifications."""

from __future__ import annotations

import asyncio

from ..client import channel_type_label
from ..logger import get_logger
from .base import CapabilityEffect, CapabilitySpec, SourcePolicy

log = get_logger(__name__)

GET_POSTS_DEFAULT_LIMIT = 30
GET_POSTS_MAX_LIMIT = 100
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


def create_get_posts_capability(mm_client, memory) -> CapabilitySpec:
    """Create the canonical cache-first channel history capability."""

    async def get_posts(
        channel_id: str,
        limit: int = GET_POSTS_DEFAULT_LIMIT,
    ) -> dict:
        posts = await asyncio.to_thread(
            _get_posts_cached,
            mm_client,
            memory,
            channel_id,
            min(limit, GET_POSTS_MAX_LIMIT),
        )
        return _format_posts(posts)

    return CapabilitySpec(
        name="get_posts",
        description=(
            "获取频道最近的消息历史。用于回顾讨论内容、总结对话、查找特定信息。"
            "优先从本地缓存读取（实时性好），缓存不足时自动从服务器拉取。"
            "返回的消息中可能含 URL — 如需 URL 对应页面的真实内容，请用 analyze_link 抓取。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "频道 ID（不是 name）；当前频道 ID 会在 user 消息中以 📍 前缀告知",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"获取消息数量 (默认 {GET_POSTS_DEFAULT_LIMIT}, 最大 {GET_POSTS_MAX_LIMIT})"
                    ),
                    "default": GET_POSTS_DEFAULT_LIMIT,
                    "maximum": GET_POSTS_MAX_LIMIT,
                },
            },
            "required": ["channel_id"],
        },
        handler=get_posts,
        effect=CapabilityEffect.READ,
        permission="mattermost:post:read",
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


def _get_posts_cached(mm_client, memory, channel_id: str, limit: int) -> list[dict]:
    """Read local messages first, falling back to Mattermost when sparse."""
    cached = memory.get_recent_messages(channel_id, limit=limit)
    cache_threshold = max(int(limit * 0.6), 3)
    if len(cached) >= cache_threshold:
        log.info(
            "get_posts: 命中本地缓存 (需要 %d 条, 缓存 %d 条)",
            limit,
            len(cached),
        )
        return cached

    log.info(
        "get_posts: 缓存不足 (需 %d 条, 缓存 %d 条), 回退 REST API",
        limit,
        len(cached),
    )
    rest_posts = mm_client.get_posts(channel_id, limit=limit)
    if rest_posts:
        for post in rest_posts:
            post["channel_id"] = channel_id
            post["username"] = mm_client.get_username(post.get("user_id", ""))
            if not memory.log_message(post):
                log.warning("get_posts 回填 message_log 失败 (id=%s)", post.get("id", "")[:12])
        log.debug("get_posts: 已回填 %d 条消息到本地缓存", len(rest_posts))
    return rest_posts if rest_posts else cached


def _format_posts(posts: list[dict]) -> dict:
    if not posts:
        return {"count": 0, "messages": []}
    messages = [
        {
            "user": post.get("username", "?"),
            "message": (post.get("message") or "")[:500],
            "time": post.get("create_at", ""),
        }
        for post in posts
    ]
    return {"count": len(messages), "messages": messages}
