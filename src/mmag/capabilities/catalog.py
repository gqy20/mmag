"""Built-in capability specifications."""

from __future__ import annotations

import asyncio

from ..client import channel_type_label
from ..logger import get_logger
from .base import CapabilityEffect, CapabilitySpec, SourcePolicy

log = get_logger(__name__)

GET_POSTS_DEFAULT_LIMIT = 30
GET_POSTS_MAX_LIMIT = 100
SEARCH_MESSAGES_DEFAULT_LIMIT = 20
SEARCH_MESSAGES_MAX_LIMIT = 50
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


def create_get_user_profile_capability(mm_client, memory) -> CapabilitySpec:
    """Create the canonical combined user-profile capability."""

    async def get_user_profile(user_id: str) -> dict:
        profile, username = await asyncio.gather(
            asyncio.to_thread(memory.get_user_profile_decoded, user_id),
            asyncio.to_thread(mm_client.get_username, user_id),
        )
        if not profile:
            return {"username": username, "note": "暂无画像信息，该用户尚未发言或画像未建立"}

        active_hours = profile.get("active_hours") or {}
        top_hours = sorted(active_hours.items(), key=lambda item: item[1], reverse=True)[:3]
        topics = profile.get("topics") or []
        return {
            "username": username,
            "message_count": profile.get("message_count", 0),
            "topics": topics[-10:] if topics else [],
            "active_hours": [f"{hour}({count}次)" for hour, count in top_hours],
            "style": profile.get("style", "未知"),
            "first_seen": profile.get("first_seen", ""),
            "last_interaction": profile.get("last_interaction", ""),
        }

    return CapabilitySpec(
        name="get_user_profile",
        description=(
            "查看用户的画像信息，包括活跃度、专业领域、偏好等。用于了解团队成员的背景和特点。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "用户 ID",
                },
            },
            "required": ["user_id"],
        },
        handler=get_user_profile,
        effect=CapabilityEffect.READ,
        permission="memory:user_profile:read",
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


def create_search_messages_capability(memory) -> CapabilitySpec:
    """Create the canonical historical-message search capability."""

    async def search_messages(
        query: str | None = None,
        channel_id: str | None = None,
        user_id: str | None = None,
        before_ts: float | None = None,
        after_ts: float | None = None,
        limit: int = SEARCH_MESSAGES_DEFAULT_LIMIT,
    ) -> dict:
        results = await asyncio.to_thread(
            memory.search_messages,
            query=query,
            channel_id=channel_id,
            user_id=user_id,
            before_ts=(before_ts / 1000.0) if before_ts is not None else None,
            after_ts=(after_ts / 1000.0) if after_ts is not None else None,
            limit=min(limit, SEARCH_MESSAGES_MAX_LIMIT),
        )
        if not results:
            return {"count": 0, "messages": [], "note": "未找到匹配消息"}
        messages = [
            {
                "channel_id": result.get("channel_id", ""),
                "user": result.get("username", "?"),
                "message": (result.get("message") or "")[:500],
                "time_ms": int((result.get("create_at") or 0) * 1000),
                "relevance_score": result.get("_score"),
            }
            for result in results
        ]
        return {"count": len(messages), "messages": messages}

    return CapabilitySpec(
        name="search_messages",
        description=(
            "按关键词/时间/用户/频道检索历史消息。"
            "用于查找'上周 X 说 Y'、'X 之前提过的方案'等回看类问题。"
            "支持中英文全文搜索 (BM25 排序),时间戳是毫秒 (Mattermost 原生格式)。"
            "channel_id 留空 = 搜全 team。query 留空 = 纯时间/用户过滤。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词 (支持中英文,留空=不过关键词)",
                },
                "channel_id": {
                    "type": "string",
                    "description": "频道 ID (留空=搜全 team)",
                },
                "user_id": {
                    "type": "string",
                    "description": "按用户 ID 过滤 (可选)",
                },
                "before_ts": {
                    "type": "number",
                    "description": "只看此时间戳(毫秒)之前的消息 (可选)",
                },
                "after_ts": {
                    "type": "number",
                    "description": "只看此时间戳(毫秒)之后的消息 (可选)",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "返回数量 "
                        f"(默认 {SEARCH_MESSAGES_DEFAULT_LIMIT}, 最大 {SEARCH_MESSAGES_MAX_LIMIT})"
                    ),
                    "default": SEARCH_MESSAGES_DEFAULT_LIMIT,
                    "maximum": SEARCH_MESSAGES_MAX_LIMIT,
                },
            },
            "required": [],
        },
        handler=search_messages,
        effect=CapabilityEffect.READ,
        permission="memory:messages:read",
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
