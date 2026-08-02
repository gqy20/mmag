"""Adapter from canonical capability specs to the governed runtime registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import CapabilityExecutor, CapabilitySpec
    from .registry import CapabilityBinding


def bind_langgraph_capability(
    spec: CapabilitySpec,
    *,
    executor: CapabilityExecutor,
) -> CapabilityBinding:
    """Expose a capability through the LangGraph runtime registry."""
    from .registry import CapabilityBinding

    return CapabilityBinding(spec, executor)
