"""External MCP tool discovery adapter for the canonical capability model."""

from __future__ import annotations

import json
from typing import Any

from .base import CapabilityEffect, CapabilitySpec, SourcePolicy


def create_mcp_capability(server_name: str, tool: Any, session: Any) -> CapabilitySpec:
    """Adapt one discovered MCP tool without leaking transport types downstream."""

    async def invoke(**arguments: Any) -> Any:
        result = await session.call_tool(tool.name, arguments=arguments)
        payload = _mcp_payload(result)
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            raise RuntimeError(payload if isinstance(payload, str) else json.dumps(payload))
        return payload

    return CapabilitySpec(
        name=f"mcp_{server_name}_{tool.name}",
        description=tool.description or f"[MCP:{server_name}] {tool.name}",
        input_schema=tool.inputSchema or {"type": "object", "properties": {}},
        handler=invoke,
        effect=_mcp_effect(getattr(tool, "annotations", None)),
        permission=f"mcp:{server_name}:{tool.name}:invoke",
        timeout_seconds=60,
        source_policy=SourcePolicy.AUTO,
    )


def _mcp_effect(annotations: Any) -> CapabilityEffect:
    if isinstance(annotations, dict):
        is_read_only = annotations.get("readOnlyHint") is True
    else:
        is_read_only = getattr(annotations, "readOnlyHint", False) is True
    return CapabilityEffect.READ if is_read_only else CapabilityEffect.WRITE


def _mcp_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if structured is not None:
        return structured

    texts = [
        item.text if hasattr(item, "text") else str(item) for item in getattr(result, "content", ())
    ]
    combined = "\n".join(texts)
    try:
        return json.loads(combined)
    except (json.JSONDecodeError, TypeError):
        return combined
