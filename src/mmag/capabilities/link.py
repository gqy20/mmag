"""Link-analysis capability and its presentation boundary."""

from __future__ import annotations

from .base import CapabilityEffect, CapabilitySpec, SourcePolicy


def create_analyze_link_capability(memory) -> CapabilitySpec:
    """Create the canonical external-link analysis capability."""

    async def analyze_link(url: str) -> dict:
        from ..url_analyzer import analyze_url

        info = await analyze_url(url, memory=memory)
        return _format_link_info(info)

    return CapabilitySpec(
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
                    "description": (
                        "要分析的完整 URL (http/https)，例如 "
                        "https://github.com/anthropics/anthropic-sdk-python"
                    ),
                },
            },
            "required": ["url"],
        },
        handler=analyze_link,
        effect=CapabilityEffect.READ,
        permission="web:read",
        timeout_seconds=30,
        source_policy=SourcePolicy.AUTO,
    )


def _format_link_info(info: dict) -> dict:
    """Format URL analyzer output for model consumption."""
    from ..url_analyzer import SUMMARY_MAX_CHARS, _truncate_summary

    kind = info.get("kind", "unknown")
    status = info.get("status", "ok")
    full_text = info.get("summary") or info.get("content") or ""
    display_summary, was_truncated = _truncate_summary(full_text, SUMMARY_MAX_CHARS)

    result = {
        "url": info.get("url", ""),
        "kind": kind,
        "status": status,
        "title": info.get("title", ""),
        "summary": display_summary,
        "cached": info.get("cached", False),
    }
    if was_truncated:
        result["full_text_length_chars"] = len(full_text)
        result["truncated"] = True

    metadata = info.get("metadata") or {}
    if kind == "github_repo" and status == "ok":
        result["stats"] = {
            "stars": metadata.get("stargazers_count"),
            "forks": metadata.get("forks_count"),
            "language": metadata.get("language"),
            "license": (
                (metadata.get("license") or {}).get("spdx_id") if metadata.get("license") else None
            ),
        }
        result["repo_info"] = {
            "full_name": metadata.get("full_name"),
            "description": metadata.get("description"),
            "topics": metadata.get("topics", []),
            "homepage": metadata.get("homepage"),
            "default_branch": metadata.get("default_branch"),
            "archived": metadata.get("archived", False),
            "private": metadata.get("private", False),
            "created_at": metadata.get("created_at"),
            "updated_at": metadata.get("updated_at"),
            "pushed_at": metadata.get("pushed_at"),
        }
    elif kind in ("github_pr", "github_issue") and status == "ok":
        result["issue_info"] = {
            "number": metadata.get("number"),
            "state": metadata.get("state"),
            "title": metadata.get("title"),
            "user": (metadata.get("user") or {}).get("login"),
            "labels": [label.get("name") for label in (metadata.get("labels") or [])],
            "created_at": metadata.get("created_at"),
            "updated_at": metadata.get("updated_at"),
            "comments": metadata.get("comments"),
        }
    elif kind == "webpage" and status == "ok":
        result["webpage"] = {
            "site_name": metadata.get("og", {}).get("site_name"),
            "image": metadata.get("og", {}).get("image"),
            "type": metadata.get("og", {}).get("type"),
            "text_length": metadata.get("full_text_length_chars"),
            "extraction_method": metadata.get("extraction_method"),
        }

    if status != "ok":
        result["error"] = info.get("error") or "未知错误"
    return result
