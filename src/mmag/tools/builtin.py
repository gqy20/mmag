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

from ..capabilities.bindings import bind_legacy_capability
from ..capabilities.catalog import (
    create_get_channel_info_capability,
    create_get_posts_capability,
    create_search_knowledge_capability,
    create_search_messages_capability,
)
from .registry import Tool

# ============================================================
# 工具参数上限（schema description / handler / 格式化 共享同一份数字）
# 改这里,所有相关地方同步更新
# ============================================================

# ============================================================
# 公共入口
# ============================================================


def build_builtin_tools(mm_client, memory) -> list[Tool]:
    """创建基于 Mattermost Client 和 Memory 的内置工具集"""

    tools = [
        _make_get_posts_tool(mm_client, memory),
        _make_search_messages_tool(memory),
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
    return bind_legacy_capability(create_get_posts_capability(mm_client, memory))


def _make_search_knowledge_tool(memory) -> Tool:
    return bind_legacy_capability(create_search_knowledge_capability(memory))


def _make_search_messages_tool(memory) -> Tool:
    return bind_legacy_capability(create_search_messages_capability(memory))


def _make_get_channel_info_tool(mm_client) -> Tool:
    return bind_legacy_capability(
        create_get_channel_info_capability(mm_client),
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
