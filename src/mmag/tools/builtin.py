"""
内置工具工厂 — 根据 MMClient 和 Memory 创建内置工具集

当前包含:
  - get_posts: 获取频道历史
  - search_knowledge: 搜索知识库
  - get_channel_info: 查询频道详情
  - save_knowledge: 写入知识库
  - get_user_profile: 查询用户画像
  - analyze_link: 分析消息中的链接 (GitHub / 通用网页)
"""

from __future__ import annotations

from ..logger import get_logger
from .registry import Tool

log = get_logger(__name__)


# ============================================================
# 公共入口
# ============================================================


def build_builtin_tools(mm_client, memory) -> list[Tool]:
    """创建基于 Mattermost Client 和 Memory 的内置工具集"""

    tools = [
        _make_get_posts_tool(mm_client, memory),
        _make_search_knowledge_tool(memory),
        _make_get_channel_info_tool(mm_client),
        _make_save_knowledge_tool(memory),
        _make_get_user_profile_tool(mm_client, memory),
        _make_analyze_link_tool(memory),
    ]
    return tools


# ============================================================
# 各工具的工厂（每个 Tool 的字段较多，独立函数更易读）
# ============================================================


def _make_get_posts_tool(mm_client, memory) -> Tool:
    return Tool(
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
                    "description": "获取消息数量 (默认 30, 最大 100)",
                    "default": 30,
                },
            },
            "required": ["channel_id"],
        },
        handler=lambda channel_id, limit=30: _format_posts(
            _get_posts_cached(mm_client, memory, channel_id, min(limit, 100))
        ),
    )


def _make_search_knowledge_tool(memory) -> Tool:
    return Tool(
        name="search_knowledge",
        description=("搜索团队知识库中的信息。用于查找之前记录的决策、流程、约定等知识。"),
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
                    "description": "返回结果数量上限 (默认 5)",
                    "default": 5,
                },
            },
            "required": ["channel_id", "query"],
        },
        handler=lambda channel_id, query, limit=5: _format_knowledge(
            memory.get_relevant_knowledge(channel_id, query, min(limit, 10))
        ),
    )


def _make_get_channel_info_tool(mm_client) -> Tool:
    return Tool(
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
        handler=lambda channel_id: _format_channel(mm_client.get_channel(channel_id)),
    )


def _make_save_knowledge_tool(memory) -> Tool:
    return Tool(
        name="save_knowledge",
        description=(
            "向团队知识库中存储一条知识。"
            "用于记住从对话中学到的重要事实、决策或结论。"
            "不要存储琐碎信息，只存有长期价值的内容。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "频道 ID（知识关联到哪个频道）",
                },
                "key": {
                    "type": "string",
                    "description": "知识的关键词/标题（如 '部署流程'）",
                },
                "value": {
                    "type": "string",
                    "description": "知识的详细内容",
                },
            },
            "required": ["channel_id", "key", "value"],
        },
        handler=lambda channel_id, key, value: _save_knowledge(memory, channel_id, key, value),
    )


def _make_get_user_profile_tool(mm_client, memory) -> Tool:
    return Tool(
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
        handler=lambda user_id: _format_profile(
            memory.get_user_profile_decoded(user_id),
            mm_client.get_username(user_id),
        ),
    )


def _make_analyze_link_tool(memory) -> Tool:
    """分析消息中链接的工具 (异步 handler，因为底层用 httpx async client)"""
    return Tool(
        name="analyze_link",
        description=(
            "分析消息中的链接内容并返回结构化摘要。"
            "GitHub 仓库 (github.com/owner/repo) 返回 stars/language/description/topics；"
            "GitHub PR/Issue 返回标题、状态、作者、创建时间；"
            "其他网页优先用 Trafilatura 提取正文（截断到 ~3000 字符，含头尾）"
            "作为 summary，提取失败/过短时回退到 og:title + og:description。"
            "结果会缓存 1 小时以避免重复请求（错误结果 5 分钟）。"
            "遇到 404/限流/网络错误时返回明确的 status 字段，调用方应据此决定回退方案。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要分析的完整 URL (http/https)，例如 https://github.com/anthropics/anthropic-sdk-python",
                },
            },
            "required": ["url"],
        },
        # handler 必须是 async，因为 ToolRegistry.execute() 会 await 它
        handler=lambda url: _analyze_link_handler(memory, url),
    )


# ============================================================
# 异步 handler：analyze_link
# ============================================================


async def _analyze_link_handler(memory, url: str) -> dict:
    """analyze_link 工具的 async handler — 委托给 url_analyzer.analyze_url"""
    # 延迟 import 避免 tools → url_analyzer → ... 循环依赖
    from ..url_analyzer import analyze_url

    info = await analyze_url(url, memory=memory)
    return _format_link_info(info)


# ============================================================
# 工具结果格式化辅助函数
# ============================================================


def _format_posts(posts: list[dict]) -> dict:
    """格式化消息列表为结构化输出"""
    if not posts:
        return {"count": 0, "messages": []}

    formatted = []
    for p in posts[-50:]:  # 最多返回 50 条
        formatted.append(
            {
                "user": p.get("username", "?"),
                "message": (p.get("message") or "")[:500],
                "time": p.get("create_at", ""),
            }
        )

    return {"count": len(formatted), "messages": formatted}


def _format_knowledge(results: list[dict]) -> dict:
    """格式化知识检索结果"""
    if not results:
        return {"count": 0, "items": [], "note": "未找到相关知识"}

    items = []
    for r in results:
        items.append(
            {
                "key": r["key"],
                "value": r["value"],
                "confidence": r.get("_score", r.get("confidence", 0)),
            }
        )

    return {"count": len(items), "items": items}


def _format_channel(ch: dict) -> dict:
    """格式化频道信息"""
    from ..client import channel_type_label

    return {
        "id": ch.get("id", ""),
        "name": ch.get("name", ""),
        "display_name": ch.get("display_name", ""),
        "type": ch.get("type", ""),
        "type_label": channel_type_label(ch.get("type", "")),
    }


def _save_knowledge(memory, channel_id: str, key: str, value: str) -> dict:
    """保存知识并返回确认"""
    memory.add_knowledge(channel_id, key, value)
    return {"status": "ok", "key": key, "message": f"已记住: {key}"}


def _format_profile(profile: dict, username: str) -> dict:
    """格式化用户画像（含自动推断的话题/时段/风格）

    依赖调用方传入已解析的 profile（topics=list, active_hours=dict），
    通常来自 Memory.get_user_profile_decoded()。
    """
    if not profile:
        return {"username": username, "note": "暂无画像信息，该用户尚未发言或画像未建立"}

    # 取最活跃的 Top 3 时段
    active_hours_raw = profile.get("active_hours") or {}
    top_hours = sorted(active_hours_raw.items(), key=lambda x: x[1], reverse=True)[:3]
    peak_hours = [f"{h}({c}次)" for h, c in top_hours] if top_hours else []

    topics = profile.get("topics") or []

    return {
        "username": username,
        "message_count": profile.get("message_count", 0),
        "topics": topics[-10:] if topics else [],  # 最近话题
        "active_hours": peak_hours,
        "style": profile.get("style", "未知"),
        "first_seen": profile.get("first_seen", ""),
        "last_interaction": profile.get("last_interaction", ""),
    }


def _format_link_info(info: dict) -> dict:
    """格式化 url_analyzer 返回的 LinkInfo 为 LLM 友好的结构

    设计:
      - 数据库 / url_analyzer 层保留完整正文（content 字段）
      - 本函数是 presentation 层：负责把全文截断到 LLM 友好的大小
      - 只暴露对回答问题有用的字段，避免 LLM 被冗余 metadata 淹没
    """
    kind = info.get("kind", "unknown")
    status = info.get("status", "ok")
    full_text = info.get("summary") or info.get("content") or ""

    # 截断只发生在 LLM 入口处 (避免把 50KB 正文一次性塞给 model)
    # 延迟 import 避免 tools → url_analyzer 循环
    from ..url_analyzer import SUMMARY_MAX_CHARS, _truncate_summary

    display_summary, was_truncated = _truncate_summary(full_text, SUMMARY_MAX_CHARS)
    full_text_length = len(full_text)

    result = {
        "url": info.get("url", ""),
        "kind": kind,
        "status": status,
        "title": info.get("title", ""),
        "summary": display_summary,
        "cached": info.get("cached", False),
    }
    # 让 LLM 知道有更多内容 (可作为是否要求深入阅读的信号)
    if was_truncated:
        result["full_text_length_chars"] = full_text_length
        result["truncated"] = True

    # 关键元数据（按 kind 分组）
    meta = info.get("metadata") or {}
    if kind == "github_repo" and status == "ok":
        result["stats"] = {
            "stars": meta.get("stargazers_count"),
            "forks": meta.get("forks_count"),
            "language": meta.get("language"),
            "license": (meta.get("license") or {}).get("spdx_id") if meta.get("license") else None,
        }
        result["repo_info"] = {
            "full_name": meta.get("full_name"),
            "description": meta.get("description"),
            "topics": meta.get("topics", []),
            "homepage": meta.get("homepage"),
            "default_branch": meta.get("default_branch"),
            "archived": meta.get("archived", False),
            "private": meta.get("private", False),
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "pushed_at": meta.get("pushed_at"),
        }
    elif kind in ("github_pr", "github_issue") and status == "ok":
        result["issue_info"] = {
            "number": meta.get("number"),
            "state": meta.get("state"),
            "title": meta.get("title"),
            "user": (meta.get("user") or {}).get("login"),
            "labels": [label.get("name") for label in (meta.get("labels") or [])],
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "comments": meta.get("comments"),
        }
    elif kind == "webpage" and status == "ok":
        result["webpage"] = {
            "site_name": meta.get("og", {}).get("site_name"),
            "image": meta.get("og", {}).get("image"),
            "type": meta.get("og", {}).get("type"),
            "text_length": meta.get("full_text_length_chars"),
            "extraction_method": meta.get("extraction_method"),  # "trafilatura" | "og_fallback"
        }

    if status != "ok":
        result["error"] = info.get("error") or "未知错误"

    return result


# ============================================================
# 缓存优先的消息获取
# ============================================================


def _get_posts_cached(mm_client, memory, channel_id: str, limit: int) -> list[dict]:
    """获取频道消息：本地缓存优先，不足时 fallback 到 REST API

    策略:
      1. 先查 SQLite message_cache（_on_posted 实时写入的）
      2. 缓存数量 >= 需求的 60% → 直接返回缓存（避免每次都打 API）
      3. 缓存不足 → 从 REST API 拉取，并回填到缓存
      4. 缓存为空 → 直接走 REST API
    """
    # 尝试从本地缓存读取
    cached = memory.get_recent_messages(channel_id, limit=limit)
    cache_threshold = max(int(limit * 0.6), 3)  # 至少 3 条或需求的 60%

    if len(cached) >= cache_threshold:
        log.info(
            "get_posts: 命中本地缓存 (需要 %d 条, 缓存 %d 条)",
            limit,
            len(cached),
        )
        return cached

    # 缓存不足，走 REST API
    log.info(
        "get_posts: 缓存不足 (需 %d 条, 缓存 %d 条), 回退 REST API",
        limit,
        len(cached),
    )
    rest_posts = mm_client.get_posts(channel_id, limit=limit)

    # 将 REST 结果回填到本地缓存（加速下次查询）
    if rest_posts:
        for p in rest_posts:
            p["channel_id"] = channel_id  # 确保有 channel_id
            p["username"] = mm_client.get_username(p.get("user_id", ""))
            memory.cache_message(p)
        log.debug("get_posts: 已回填 %d 条消息到本地缓存", len(rest_posts))

    return rest_posts if rest_posts else cached
