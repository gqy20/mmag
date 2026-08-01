"""Adapter from canonical capability specs to the governed runtime registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import CapabilityExecutor, CapabilitySpec

if TYPE_CHECKING:
    from .registry import CapabilityBinding


def bind_langgraph_capability(
    spec: CapabilitySpec,
    *,
    executor: CapabilityExecutor | None = None,
) -> CapabilityBinding:
    """Expose a capability through the LangGraph runtime registry."""
    from .registry import CapabilityBinding

    runner = executor or CapabilityExecutor()

    async def handler(**arguments: Any) -> Any:
        result = await runner.execute(spec, arguments)
        return result.to_payload()

    return CapabilityBinding(
        name=spec.name,
        description=spec.description,
        input_schema=spec.input_schema,
        handler=handler,
        capability=spec,
        executor=runner,
    )
