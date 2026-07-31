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

from .logger import get_logger

log = get_logger(__name__)

# ============================================================
# 工具参数上限（与 builtin.py 保持同一份数字）
# ============================================================

GET_POSTS_DEFAULT_LIMIT = 30
GET_POSTS_MAX_LIMIT = 100
SEARCH_KNOWLEDGE_DEFAULT_LIMIT = 5
SEARCH_KNOWLEDGE_MAX_LIMIT = 10
SEARCH_MESSAGES_DEFAULT_LIMIT = 20
SEARCH_MESSAGES_MAX_LIMIT = 50

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
    @tool(
        "get_posts",
        (
            "获取频道最近的消息历史。用于回顾讨论内容、总结对话、查找特定信息。"
            "优先从本地缓存读取（实时性好），缓存不足时自动从服务器拉取。"
            "返回的消息中可能含 URL — 如需 URL 对应页面的真实内容，请用 analyze_link 抓取。"
        ),
        {"channel_id": str, "limit": int},
    )
    async def sdk_get_posts(args):
        channel_id = args["channel_id"]
        limit = args.get("limit", GET_POSTS_DEFAULT_LIMIT)
        result = await asyncio.to_thread(
            _get_posts_cached,
            mm_client,
            memory,
            channel_id,
            min(limit, GET_POSTS_MAX_LIMIT),
        )
        formatted = await asyncio.to_thread(_format_posts, result)
        return _sdk_tool_return(formatted)

    return sdk_get_posts


def _make_sdk_search_messages(memory):
    @tool(
        "search_messages",
        (
            "按关键词/时间/用户/频道检索历史消息。"
            "用于查找'上周 X 说 Y'、'X 之前提过的方案'等回看类问题。"
            "支持中英文全文搜索 (BM25 排序),时间戳是毫秒 (Mattermost 原生格式)。"
            "channel_id 留空 = 搜全 team。query 留空 = 纯时间/用户过滤。"
        ),
        {
            "query": str,
            "channel_id": str,
            "user_id": str,
            "before_ts": float,
            "after_ts": float,
            "limit": int,
        },
    )
    async def sdk_search_messages(args):
        result = await asyncio.to_thread(
            memory.search_messages,
            query=args.get("query"),
            channel_id=args.get("channel_id"),
            user_id=args.get("user_id"),
            before_ts=(args["before_ts"] / 1000.0) if args.get("before_ts") else None,
            after_ts=(args["after_ts"] / 1000.0) if args.get("after_ts") else None,
            limit=min(args.get("limit", SEARCH_MESSAGES_DEFAULT_LIMIT), SEARCH_MESSAGES_MAX_LIMIT),
        )
        formatted = await asyncio.to_thread(_format_search_results, result)
        return _sdk_tool_return(formatted)

    return sdk_search_messages


def _make_sdk_search_knowledge(memory):
    @tool(
        "search_knowledge",
        "搜索团队知识库中的信息。用于查找之前记录的决策、流程、约定等知识。",
        {"channel_id": str, "query": str, "limit": int},
    )
    async def sdk_search_knowledge(args):
        result = await asyncio.to_thread(
            memory.get_relevant_knowledge,
            args["channel_id"],
            args["query"],
            min(args.get("limit", SEARCH_KNOWLEDGE_DEFAULT_LIMIT), SEARCH_KNOWLEDGE_MAX_LIMIT),
        )
        formatted = await asyncio.to_thread(_format_knowledge, result)
        return _sdk_tool_return(formatted)

    return sdk_search_knowledge


def _make_sdk_get_channel_info(mm_client):
    @tool(
        "get_channel_info",
        "获取频道的详细信息，包括名称、类型、成员数等。用于了解当前所在频道的基本信息。",
        {"channel_id": str},
    )
    async def sdk_get_channel_info(args):
        ch = await asyncio.to_thread(mm_client.get_channel, args["channel_id"])
        formatted = await asyncio.to_thread(_format_channel, ch)
        return _sdk_tool_return(formatted)

    return sdk_get_channel_info


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
    @tool(
        "get_user_profile",
        "查看用户的画像信息，包括活跃度、专业领域、偏好等。用于了解团队成员的背景和特点。",
        {"user_id": str},
    )
    async def sdk_get_user_profile(args):
        profile = await asyncio.to_thread(memory.get_user_profile_decoded, args["user_id"])
        username = await asyncio.to_thread(mm_client.get_username, args["user_id"])
        formatted = await asyncio.to_thread(_format_profile, profile, username)
        return _sdk_tool_return(formatted)

    return sdk_get_user_profile


def _make_sdk_analyze_link(memory):
    @tool(
        "analyze_link",
        (
            "分析消息中的链接内容并返回结构化摘要。"
            "GitHub 仓库 (github.com/owner/repo) 返回 stars/language/description/topics；"
            "GitHub PR/Issue 返回标题、状态、作者、创建时间；"
            "其他网页优先用 Trafilatura 提取正文（截断到 ~3000 字符，含头尾）"
            "作为 summary，提取失败/过短时回退到 og:title + og:description。"
            "结果会缓存 1 小时以避免重复请求（错误结果 5 分钟）。"
            "遇到 404/限流/网络错误时返回明确的 status 字段，调用方应据此决定回退方案。"
        ),
        {"url": str},
    )
    async def sdk_analyze_link(args):
        # 原生 async handler (httpx.AsyncClient)，无需 to_thread
        from ..url_analyzer import analyze_url

        info = await analyze_url(args["url"], memory=memory)
        formatted = _format_link_info(info)
        # analyze_link 是唯一含外部 URL 数据的工具 → 注入 _sources
        enriched = _enrich_with_sources(formatted, "analyze_link", args)
        return _sdk_tool_return(enriched)

    return sdk_analyze_link


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


# ============================================================
# 来源元数据提取（从 registry.py 搬运）
# ============================================================


def _enrich_with_sources(result: Any, tool_name: str, input_data: dict) -> Any:
    """为工具结果添加结构化来源元数据（仅当结果包含可识别的外部来源时）

    从 registry.py 原样搬运，仅保留 analyze_link 需要的 dict 分支。
    """
    sources: list[dict[str, Any]] = []

    if isinstance(result, dict) and result.get("url") and result.get("title"):
        src: dict[str, Any] = {
            "url": result["url"],
            "title": result["title"],
            "tool": tool_name,
        }
        kind = result.get("kind", "")
        if kind:
            src["kind"] = kind
        for meta_key in ("repo_info", "issue_info"):
            meta = result.get(meta_key)
            if isinstance(meta, dict):
                if meta.get("created_at"):
                    src["date"] = meta["created_at"]
                if meta.get("full_name"):
                    src["repo"] = meta["full_name"]
                if meta.get("user"):
                    src["author"] = meta["user"]
                break
        sources.append(src)

    if sources and isinstance(result, dict):
        result["_sources"] = sources

    return result


# ============================================================
# 工具结果格式化辅助函数（从 builtin.py 原样搬运）
# ============================================================


def _format_posts(posts: list[dict]) -> dict:
    """格式化消息列表为结构化输出"""
    if not posts:
        return {"count": 0, "messages": []}

    messages = [
        {
            "user": p.get("username", "?"),
            "message": (p.get("message") or "")[:500],
            "time": p.get("create_at", ""),
        }
        for p in posts
    ]

    return {"count": len(messages), "messages": messages}


def _format_search_results(results: list[dict]) -> dict:
    """格式化 search_messages 检索结果"""
    if not results:
        return {"count": 0, "messages": [], "note": "未找到匹配消息"}

    messages = [
        {
            "channel_id": r.get("channel_id", ""),
            "user": r.get("username", "?"),
            "message": (p.get("message") or "")[:500] if (p := r) else "",
            "time_ms": int((r.get("create_at") or 0) * 1000),
            "relevance_score": r.get("_score"),
        }
        for r in results
    ]
    return {"count": len(messages), "messages": messages}


def _format_knowledge(results: list[dict]) -> dict:
    """格式化知识检索结果"""
    if not results:
        return {"count": 0, "items": [], "note": "未找到相关知识"}

    items = [
        {
            "key": r["key"],
            "value": r["value"],
            "confidence": r.get("_score", r.get("confidence", 0)),
        }
        for r in results
    ]

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
    """格式化用户画像"""
    if not profile:
        return {"username": username, "note": "暂无画像信息，该用户尚未发言或画像未建立"}

    active_hours_raw = profile.get("active_hours") or {}
    top_hours = sorted(active_hours_raw.items(), key=lambda x: x[1], reverse=True)[:3]
    peak_hours = [f"{h}({c}次)" for h, c in top_hours] if top_hours else []

    topics = profile.get("topics") or []

    return {
        "username": username,
        "message_count": profile.get("message_count", 0),
        "topics": topics[-10:] if topics else [],
        "active_hours": peak_hours,
        "style": profile.get("style", "未知"),
        "first_seen": profile.get("first_seen", ""),
        "last_interaction": profile.get("last_interaction", ""),
    }


def _format_link_info(info: dict) -> dict:
    """格式化 url_analyzer 返回的 LinkInfo 为 LLM 友好的结构"""
    kind = info.get("kind", "unknown")
    status = info.get("status", "ok")
    full_text = info.get("summary") or info.get("content") or ""

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
    if was_truncated:
        result["full_text_length_chars"] = full_text_length
        result["truncated"] = True

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
            "extraction_method": meta.get("extraction_method"),
        }

    if status != "ok":
        result["error"] = info.get("error") or "未知错误"

    return result


# ============================================================
# 缓存优先的消息获取（从 builtin.py 原样搬运）
# ============================================================


def _get_posts_cached(mm_client, memory, channel_id: str, limit: int) -> list[dict]:
    """获取频道消息：本地 message_log 优先，不足时 fallback 到 REST API"""
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
        for p in rest_posts:
            p["channel_id"] = channel_id
            p["username"] = mm_client.get_username(p.get("user_id", ""))
            if not memory.log_message(p):
                log.warning("get_posts 回填 message_log 失败 (id=%s)", p.get("id", "")[:12])
        log.debug("get_posts: 已回填 %d 条消息到本地缓存", len(rest_posts))

    return rest_posts if rest_posts else cached
