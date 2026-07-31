"""Shared source metadata normalization for every capability binding."""

from __future__ import annotations

import json
import re
from typing import Any

_URL_PATTERN = re.compile(r"^https?://\S+", re.IGNORECASE)


def enrich_with_sources(
    result: Any,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    """Attach normalized ``_sources`` while preserving the input representation."""
    if isinstance(result, dict) and result.get("_sources"):
        return result

    is_json_text = isinstance(result, str)
    data = _parse_json(result) if is_json_text else result
    enriched: Any
    if isinstance(data, list):
        enriched = _enrich_batch(data, tool_name, arguments)
    elif isinstance(data, dict):
        enriched = _enrich_mapping(data, tool_name, arguments)
    else:
        return result

    if is_json_text:
        if enriched is data:
            return result
        return json.dumps(enriched, ensure_ascii=False, default=str)
    return enriched


def _parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _enrich_mapping(
    data: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    sources = _mapping_sources(data, tool_name, arguments)
    if not sources:
        return data
    enriched = dict(data)
    enriched["_sources"] = sources
    return enriched


def _mapping_sources(
    data: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
) -> list[dict[str, Any]]:
    if data.get("url") and data.get("title"):
        return [_direct_source(data, tool_name)]
    if isinstance(data.get("results"), list):
        return [
            source
            for item in data["results"]
            if isinstance(item, dict)
            and (source := _result_item_source(item, tool_name)) is not None
        ]
    search_results = data.get("search_results")
    if isinstance(search_results, dict) and isinstance(search_results.get("results"), list):
        return [
            source
            for item in search_results["results"]
            if isinstance(item, dict)
            and (source := _image_source(item, tool_name)) is not None
        ]
    if data.get("title") and (url := _input_url(arguments)):
        return [{"url": url, "title": data["title"], "tool": tool_name}]
    return []


def _direct_source(data: dict[str, Any], tool_name: str) -> dict[str, Any]:
    source: dict[str, Any] = {
        "url": data["url"],
        "title": data["title"],
        "tool": tool_name,
    }
    if data.get("kind"):
        source["kind"] = data["kind"]
    for metadata_key in ("repo_info", "issue_info"):
        metadata = data.get(metadata_key)
        if not isinstance(metadata, dict):
            continue
        for source_key, metadata_key in (
            ("date", "created_at"),
            ("repo", "full_name"),
            ("author", "user"),
        ):
            if metadata.get(metadata_key):
                source[source_key] = metadata[metadata_key]
        break
    return source


def _result_item_source(
    item: dict[str, Any],
    tool_name: str,
) -> dict[str, Any] | None:
    url = item.get("url") or item.get("href")
    if not url:
        for key in ("link", "content"):
            candidate = item.get(key)
            if candidate and _looks_like_url(str(candidate)):
                url = candidate
                break
    if not url or not item.get("title"):
        return None
    source: dict[str, Any] = {
        "url": str(url),
        "title": item["title"],
        "tool": tool_name,
    }
    if item.get("date"):
        source["date"] = item["date"]
    if item.get("source"):
        source["source_site"] = item["source"]
    return source


def _image_source(item: dict[str, Any], tool_name: str) -> dict[str, Any] | None:
    url = item.get("image") or item.get("thumbnail")
    if not url or not item.get("title"):
        return None
    source: dict[str, Any] = {
        "url": url,
        "title": item["title"],
        "tool": tool_name,
    }
    if item.get("source"):
        source["source_site"] = item["source"]
    return source


def _enrich_batch(
    data: list[Any],
    tool_name: str,
    arguments: dict[str, Any],
) -> list[Any]:
    urls = arguments.get("urls", [])
    if not isinstance(urls, list):
        return data
    sources = [
        {"url": urls[index], "title": item["title"], "tool": tool_name}
        for index, item in enumerate(data)
        if isinstance(item, dict)
        and item.get("title")
        and index < len(urls)
        and _looks_like_url(str(urls[index]))
    ]
    return [*data, {"_sources": sources}] if sources else data


def _input_url(arguments: dict[str, Any]) -> str | None:
    url = arguments.get("url")
    if isinstance(url, str) and _looks_like_url(url):
        return url
    urls = arguments.get("urls")
    if isinstance(urls, list) and urls:
        first = urls[0]
        if isinstance(first, str) and _looks_like_url(first):
            return first
    return None


def _looks_like_url(value: str) -> bool:
    return bool(_URL_PATTERN.match(value.strip()))
