"""
SDK @tool 定义 — 8 个 crawl-mcp 工具的 in-process 包装。

原因: SDK 外部 .mcp.json 子进程模式 (uvx crawl-mcp) 在 stdio 下不响应，
导致 WaitForMcpServers 超时。解决方案: 直接导入 crawl4ai_mcp 的 FastMCP
实例，通过 mcp.call_tool() 同进程调用，包装为 @tool 函数。

公共入口:
  create_sdk_crawl_tools() -> list[callable]
    返回 8 个 @tool-decorated 函数，可直接传给 create_sdk_mcp_server(tools=[...])
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from .logger import get_logger

log = get_logger(__name__)


# ============================================================
# 公共入口
# ============================================================


def create_sdk_crawl_tools() -> list:
    """创建 8 个 crawl-mcp @tool-decorated 函数 (in-process 包装)。

    通过直接调用 crawl4ai_mcp.fastmcp_server.mcp.call_tool() 实现，
    不需要启动外部子进程。
    """
    return [
        _make_crawl_single(),
        _make_crawl_site(),
        _make_crawl_batch(),
        _make_search_text(),
        _make_search_news(),
        _make_search_books(),
        _make_search_videos(),
        _make_search_images(),
    ]


# ============================================================
# 核心调用桥接
# ============================================================


def _get_crawl_mcp_instance():
    """获取 crawl4ai_mcp FastMCP 实例（带多路径回退）。

    优先级:
      1. 项目 venv (pip install crawl-mcp)
      2. uvx 缓存路径 (uvx crawl-mcp 自动安装的位置)
    """
    import sys

    # 路径 1: 正常导入
    try:
        from crawl4ai_mcp.fastmcp_server import mcp as _mcp

        return _mcp
    except ImportError:
        pass

    # 路径 2: uvx 缓存 (crawl-mcp 通过 uvx 安装的独立环境)
    #   支持两种缓存结构:
    #     A) 新版 venv: {cache}/{hash}/lib/python3.XX/site_packages/crawl4ai_mcp/
    #     B) 旧版直装: {cache}/{hash}/crawl4ai_mcp/ (依赖也在同级)
    _uvx_base = Path.home() / ".cache" / "uv" / "archive-v0"
    if _uvx_base.exists():
        import glob as _glob_mod

        _py_short = f"python3.{sys.version_info.minor}"

        # 模式 A: 新版 venv 结构 (site_packages 下有完整依赖)
        _pattern_a = str(_uvx_base / f"*/lib/{_py_short}/site_packages/crawl4ai_mcp")
        for _match in sorted(_glob_mod.glob(_pattern_a), reverse=True):
            _sp = Path(_match).parent  # site_packages
            if str(_sp) not in sys.path:
                sys.path.insert(0, str(_sp))
            try:
                from crawl4ai_mcp.fastmcp_server import mcp as _mcp

                log.info("从 uvx 缓存加载 crawl-mcp: %s", _sp)
                return _mcp
            except ImportError:
                if str(_sp) in sys.path:
                    sys.path.remove(str(_sp))
                continue

        # 模式 B: 旧版结构 (crawl4ai_mcp 在根目录，需找同级的 lib/python*/site_packages)
        _pattern_b = str(_uvx_base / "*/crawl4ai_mcp")
        for _match in sorted(_glob_mod.glob(_pattern_b), reverse=True):
            _candidate_root = Path(_match).parent  # {cache}/{hash}/
            # 在同级找 lib/python*/site_packages
            _sp_candidate = _candidate_root / "lib" / _py_short / "site_packages"
            if _sp_candidate.exists() and (_sp_candidate / "crawl4ai").exists():
                _sp = _sp_candidate
            else:
                # 降级：直接用候选根目录（可能不完整）
                _sp = _candidate_root

            if str(_sp) not in sys.path:
                sys.path.insert(0, str(_sp))
            try:
                from crawl4ai_mcp.fastmcp_server import mcp as _mcp

                log.info("从 uvx 缓存加载 crawl-mcp [旧格式]: %s", _sp)
                return _mcp
            except ImportError:
                if str(_sp) in sys.path:
                    sys.path.remove(str(_sp))
                continue

    raise ImportError(
        "crawl4ai_mcp 未安装。请运行: pip install crawl-mcp 或 uvx crawl-mcp"
    )


async def _call_crawl_tool(tool_name: str, arguments: dict[str, Any]) -> dict:
    """调用 crawl4ai_mcp FastMCP 实例的工具方法。

    Returns:
        解析后的 JSON dict 或原始错误信息
    """
    try:
        _crawl_mcp = _get_crawl_mcp_instance()

        log.debug("调用 crawl-mcp: %s(%s)", tool_name, list(arguments.keys()))
        result = await _crawl_mcp.call_tool(tool_name, arguments)

        # FastMCP ToolResult → 提取 text content
        if hasattr(result, "content") and result.content:
            texts = []
            for item in result.content:
                if hasattr(item, "text") and item.text:
                    texts.append(item.text)
            combined = "\n".join(texts)
            # 尝试解析 JSON
            try:
                return json.loads(combined)
            except (json.JSONDecodeError, TypeError):
                return {"raw": combined}
        elif hasattr(result, "structured_content") and result.structured_content:
            return result.structured_content
        else:
            return {"raw": str(result)}
    except ImportError as e:
        log.error("crawl4ai_mcp 未安装: %s", e)
        return {"error": f"crawl-mcp 包未安装: {e}"}
    except Exception as e:
        log.error("crawl-mcp 调用失败 [%s]: %s", tool_name, e, exc_info=True)
        return {"error": str(e)}


# ============================================================
# 工具工厂 — 参数 schema 从 crawl4ai_mcp 真实定义获取
# ============================================================


def _make_crawl_single():
    @tool(
        "crawl_single",
        (
            "爬取单个网页（自动降级：快速提取 → 浏览器渲染）。"
            "适用于已经知道明确 URL、需要提取页面正文 Markdown 的场景。"
        ),
        {
            "url": str,
            "enhanced": bool,
            "llm_config": str,
            "prefer_fast": bool,
            "min_content_length": int,
        },
    )
    async def sdk_crawl_single(args):
        result = await _call_crawl_tool("crawl_single", args)
        return _sdk_tool_return(result)

    return sdk_crawl_single


def _make_crawl_site():
    @tool(
        "crawl_site",
        (
            "从入口页开始递归爬取站内页面（浏览器 + BFS 深度爬取）。"
            "适用于只有一个网站入口、希望沿站内链接抓取若干页面的场景。"
        ),
        {
            "url": str,
            "depth": int,
            "pages": int,
            "concurrent": int,
        },
    )
    async def sdk_crawl_site(args):
        result = await _call_crawl_tool("crawl_site", args)
        return _sdk_tool_return(result)

    return sdk_crawl_site


def _make_crawl_batch():
    @tool(
        "crawl_batch",
        (
            "批量爬取多个网页（自动降级版）。"
            "适用于已经有一组明确 URL、需要并行抓取多个页面的场景。"
        ),
        {
            "urls": str,  # JSON array string
            "concurrent": int,
            "llm_config": str,
            "llm_concurrent": int,
            "prefer_fast": bool,
            "min_content_length": int,
        },
    )
    async def sdk_crawl_batch(args):
        result = await _call_crawl_tool("crawl_batch", args)
        return _sdk_tool_return(result)

    return sdk_crawl_batch


def _make_search_text():
    @tool(
        "search_text",
        (
            "搜索网页内容（通用搜索）。"
            "适用于搜索技术文档、百科、博客、论坛、教程等网页内容。"
            "只返回搜索结果摘要和链接，不抓取页面正文；如需正文请对结果 URL 再调用 crawl_single。"
        ),
        {
            "query": str,
            "region": str,
            "safesearch": str,
            "max_results": int,
        },
    )
    async def sdk_search_text(args):
        result = await _call_crawl_tool("search_text", args)
        return _sdk_tool_return(result)

    return sdk_search_text


def _make_search_news():
    @tool(
        "search_news",
        (
            "搜索新闻内容。"
            "适用于搜索突发新闻、时事、财经、体育等时效性内容。"
        ),
        {
            "query": str,
            "region": str,
            "safesearch": str,
            "max_results": int,
        },
    )
    async def sdk_search_news(args):
        result = await _call_crawl_tool("search_news", args)
        return _sdk_tool_return(result)

    return sdk_search_news


def _make_search_books():
    @tool(
        "search_books",
        "搜索图书。适用于查找技术书籍、学术资料、电子书等。",
        {
            "query": str,
            "region": str,
            "max_results": int,
        },
    )
    async def sdk_search_books(args):
        result = await _call_crawl_tool("search_books", args)
        return _sdk_tool_return(result)

    return sdk_search_books


def _make_search_videos():
    @tool(
        "search_videos",
        "查找视频。适用于查找教程视频、演示视频、课程录像等。",
        {
            "query": str,
            "region": str,
            "safesearch": str,
            "max_results": int,
        },
    )
    async def sdk_search_videos(args):
        result = await _call_crawl_tool("search_videos", args)
        return _sdk_tool_return(result)

    return sdk_search_videos


def _make_search_images():
    @tool(
        "search_images",
        "搜索图片（支持下载和分析）。",
        {
            "query": str,
            "region": str,
            "safesearch": str,
            "max_results": int,
            "size": str,
            "color": str,
            "type_image": str,
            "layout": str,
            "download": bool,
            "download_count": int,
            "output_dir": str,
            "analyze": bool,
            "analysis_prompt": str,
            "analyze_concurrent": int,
        },
    )
    async def sdk_search_images(args):
        result = await _call_crawl_tool("search_images", args)
        return _sdk_tool_return(result)

    return sdk_search_images


# ============================================================
# SDK 工具返回格式（与 sdk_tools.py 一致）
# ============================================================


def _sdk_tool_return(result_data: Any) -> dict:
    """包装工具结果为 SDK 要求的格式。"""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result_data, ensure_ascii=False),
            }
        ]
    }
