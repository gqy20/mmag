"""
Tool dataclass + ToolRegistry — 工具抽象与执行

工具是 LLM 可以自主调用的函数，每个工具包含:
- name: 工具名（LLM 通过名称调用）
- description: 自然语言描述（LLM 靠此决定何时调用）
- input_schema: JSON Schema（校验输入参数）
- handler: 实际执行函数（支持 sync / async）
"""

from __future__ import annotations

import inspect
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

from ..logger import get_logger, trace

log = get_logger(__name__)


# ============================================================
# 来源元数据提取 — 为外部数据工具自动注入 _sources 字段
# ============================================================


def _enrich_with_sources(result: Any, tool_name: str, input_data: dict) -> Any:
    """为工具结果添加结构化来源元数据（仅当结果包含可识别的外部来源时）

    设计原则：
      - 只处理确实含外部数据的工具，纯本地工具（get_posts 等）零开销
      - 内置工具（dict）和 MCP 工具（JSON 字符串）统一处理
      - 提取失败时静默降级，不影响原有结果
      - 注入的 _sources 字段供 LLM 构建规范引用（见 prompts.yml 引用规范）

    Args:
        result: 工具 handler 原始返回值（dict / str / 其他）
        tool_name: 工具名称（用于判断提取策略）
        input_data: 工具调用时的输入参数（crawl 类工具需要从中取 url）

    Returns:
        增强后的结果（dict 会原地修改并返回；str 会重新序列化；其他原样返回）
    """
    sources: list[dict[str, Any]] = []

    # ════════════════════════════════════════════════
    # A. 内置工具：dict 输入，直接读字段
    # ════════════════════════════════════════════════
    if isinstance(result, dict):
        # analyze_link: {url, title, kind, ...}
        if result.get("url") and result.get("title"):
            source: dict[str, Any] = {
                "url": result["url"],
                "title": result["title"],
                "tool": tool_name,
            }
            kind = result.get("kind", "")
            if kind:
                source["kind"] = kind
            # GitHub 特有元数据
            for meta_key in ("repo_info", "issue_info"):
                meta = result.get(meta_key)
                if isinstance(meta, dict):
                    if meta.get("created_at"):
                        source["date"] = meta["created_at"]
                    if meta.get("full_name"):
                        source["repo"] = meta["full_name"]
                    if meta.get("user"):
                        source["author"] = meta["user"]
                    break
            sources.append(source)

    # ════════════════════════════════════════════════
    # B. MCP 工具：str 输入（JSON 字符串），需要解析
    # ════════════════════════════════════════════════
    elif isinstance(result, str):
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result  # 不是 JSON，原样返回（不应发生但防御性处理）

        # crawl_batch 返回的是 list[dict] 而非 dict
        if isinstance(data, list):
            return _enrich_batch_result(data, tool_name, input_data)

        if not isinstance(data, dict):
            return result

        # --- 搜索类：results 数组 ---
        if isinstance(data.get("results"), list):
            for r in data["results"]:
                if not isinstance(r, dict):
                    continue
                src = _extract_source_from_result_item(r, tool_name)
                if src:
                    sources.append(src)

        # --- 爬取类：{title} + URL 来自输入参数 ---
        elif data.get("title"):
            url = _extract_url_from_input(input_data)
            if url:
                sources.append({
                    "url": url,
                    "title": data["title"],
                    "tool": tool_name,
                })

        # --- search_images 特殊结构：search_results.results[] ---
        elif (
            isinstance(data.get("search_results"), dict)
            and isinstance(data["search_results"].get("results"), list)
        ):
            for r in data["search_results"]["results"]:
                if not isinstance(r, dict):
                    continue
                img_url = r.get("image") or r.get("thumbnail")
                title = r.get("title")
                if img_url and title:
                    src = {
                        "url": img_url,
                        "title": title,
                        "tool": tool_name,
                    }
                    if r.get("source"):
                        src["source_site"] = r["source"]
                    sources.append(src)

        # 注入 _sources 并返回增强后的 JSON 字符串
        if sources:
            data["_sources"] = sources
            return json.dumps(data, ensure_ascii=False, default=str)

        return result  # 无来源可提取，返回原始字符串

    # ════════════════════════════════════════════════
    # C. 注入到 dict 结果中
    # ════════════════════════════════════════════════
    if sources and isinstance(result, dict):
        result["_sources"] = sources

    return result


def _extract_source_from_result_item(item: dict, tool_name: str) -> dict | None:
    """从搜索结果的单个 item 中提取来源信息

    覆盖 DDGS 返回的各种字段命名差异：
      - search_text:  {href, title, body}
      - search_news:  {url, title, date, source, body}
      - search_books: {url/href, title, body} (字段名不确定)
      - search_videos: {url/content, title} (字段名不确定)
    """
    # search_news: 最完整格式 {url, title, date, source}
    if item.get("url") and item.get("title"):
        src: dict[str, Any] = {
            "url": item["url"],
            "title": item["title"],
            "tool": tool_name,
        }
        if item.get("date"):
            src["date"] = item["date"]
        if item.get("source"):
            src["source_site"] = item["source"]
        return src

    # search_text: {href, title} （注意字段名是 href 不是 url!）
    if item.get("href") and item.get("title"):
        return {
            "url": item["href"],
            "title": item["title"],
            "tool": tool_name,
        }

    # books/videos 的兜底：尝试常见字段名
    for url_key in ("url", "link", "content"):
        url_val = item.get(url_key)
        if url_val and _looks_like_url(str(url_val)) and item.get("title"):
            return {
                "url": str(url_val),
                "title": item["title"],
                "tool": tool_name,
            }

    return None


def _enrich_batch_result(data: list, tool_name: str, input_data: dict) -> str:
    """处理 crawl_batch 返回的 list[dict] 格式

    crawl_batch 返回 [{success, markdown, title}, ...]，
    URL 来自 input_data.urls 列表，按索引一一对应。
    """
    urls = input_data.get("urls", [])
    if not isinstance(urls, list):
        urls = []

    sources = []
    for i, item in enumerate(data):
        if isinstance(item, dict) and item.get("title"):
            url = urls[i] if i < len(urls) else None
            if url and _looks_like_url(str(url)):
                sources.append({
                    "url": url,
                    "title": item["title"],
                    "tool": tool_name,
                })

    if sources:
        data.append({"_sources": sources})  # 追加到列表末尾（不影响原有结构）

    return json.dumps(data, ensure_ascii=False, default=str)


def _extract_url_from_input(input_data: dict) -> str | None:
    """从工具输入参数中提取 URL（用于 crawl_single/batch/site）

    crawl_single:  {url: "..."}
    crawl_batch:   {urls: ["..."]}
    crawl_site:    {url: "..."}
    """
    # 直接的 url 参数
    url = input_data.get("url")
    if url and isinstance(url, str) and _looks_like_url(url):
        return url

    # urls 列表（batch 场景取第一个）
    urls = input_data.get("urls")
    if isinstance(urls, list) and urls:
        first = urls[0]
        if isinstance(first, str) and _looks_like_url(first):
            return first

    return None


_URL_PATTERN = re.compile(r"^https?://\S+", re.IGNORECASE)


def _looks_like_url(s: str) -> bool:
    """宽松的 URL 检测（不验证可达性，只看格式）"""
    return bool(_URL_PATTERN.match(s.strip()))


@dataclass
class Tool:
    """单个工具定义"""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[..., Any]


class ToolRegistry:
    """工具注册表 — 管理所有可用工具"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具"""
        if tool.name in self._tools:
            log.warning("工具 '%s' 已存在，将被覆盖", tool.name)
        self._tools[tool.name] = tool
        log.info(
            "工具已注册: %s (%d 参数)", tool.name, len(tool.input_schema.get("properties", {}))
        )

    def unregister(self, name: str) -> bool:
        """注销工具，返回是否存在并被删除"""
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def unregister_prefix(self, prefix: str) -> int:
        """注销所有 name 以 prefix 开头的工具，返回注销数量"""
        names = [n for n in self._tools if n.startswith(prefix)]
        for n in names:
            del self._tools[n]
        return len(names)

    def get_all(self) -> list[Tool]:
        """获取所有已注册的工具"""
        return list(self._tools.values())

    def get_schema_list(self) -> list[dict[str, Any]]:
        """获取 Anthropic API 格式的工具定义列表（用于 LLM 调用）"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    async def execute(self, name: str, input_data: dict[str, Any]) -> str:
        """
        执行指定工具并返回结果字符串。

        Returns:
            工具执行的 JSON 字符串结果，或错误信息。
        """
        tool = self._tools.get(name)
        if not tool:
            log.warning("%s 未知工具: %s", trace.prefix(), name)
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

        t0 = time.monotonic()
        log.info(
            "%s 调用工具: %s(%s)",
            trace.prefix(),
            name,
            json.dumps(input_data, ensure_ascii=False)[:200],
        )

        try:
            result = tool.handler(**input_data)
            # async generator 不是 awaitable，需要先逐项收集。
            if inspect.isasyncgen(result):
                result = [item async for item in result]
            elif inspect.isawaitable(result):
                result = await result

            # 为外部数据工具注入结构化来源元数据（_sources 字段）
            # 本地工具（get_posts 等）会静默跳过，零额外开销
            result = _enrich_with_sources(result, name, input_data)

            # 统一序列化为 JSON 字符串
            if isinstance(result, (dict, list)):
                result_str = json.dumps(result, ensure_ascii=False, default=str)
            elif isinstance(result, str):
                result_str = result
            else:
                result_str = json.dumps({"result": result}, ensure_ascii=False, default=str)

            elapsed = time.monotonic() - t0
            log.info(
                "%s 工具完成: %s (%.3fs, 结果 %d 字符)",
                trace.prefix(),
                name,
                elapsed,
                len(result_str),
            )
            return result_str

        except Exception as e:
            elapsed = time.monotonic() - t0
            log.error(
                "%s 工具 '%s' 执行失败 (%.3fs): %s", trace.prefix(), name, elapsed, e, exc_info=True
            )
            return json.dumps(
                {"error": f"工具执行错误: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )
