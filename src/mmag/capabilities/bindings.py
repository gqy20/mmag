"""Adapters from capability specs to the legacy and SDK tool surfaces."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import tool

from .base import CapabilityExecutor, CapabilitySpec

if TYPE_CHECKING:
    from ..tools.registry import Tool


def bind_legacy_capability(
    spec: CapabilitySpec,
    *,
    executor: CapabilityExecutor | None = None,
) -> Tool:
    """Expose a capability through the legacy ToolRegistry contract."""
    from ..tools.registry import Tool

    runner = executor or CapabilityExecutor()

    async def handler(**arguments: Any) -> Any:
        result = await runner.execute(spec, arguments)
        return result.to_payload()

    return Tool(
        name=spec.name,
        description=spec.description,
        input_schema=spec.input_schema,
        handler=handler,
        capability=spec,
        executor=runner,
    )


def bind_sdk_capability(
    spec: CapabilitySpec,
    *,
    executor: CapabilityExecutor | None = None,
):
    """Expose a capability through Claude Agent SDK's ``@tool`` contract."""
    runner = executor or CapabilityExecutor()

    @tool(spec.name, spec.description, dict(spec.input_schema))
    async def sdk_handler(arguments: dict[str, Any]):
        result = await runner.execute(spec, arguments)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result.to_payload(), ensure_ascii=False, default=str),
                }
            ]
        }

    return sdk_handler
