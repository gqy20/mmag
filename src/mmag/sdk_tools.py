"""SDK bindings generated from the single built-in capability catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .capabilities import (
    CapabilityContext,
    bind_sdk_capability,
    create_builtin_capabilities,
    get_capability_context,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def create_sdk_tools(
    mm_client,
    memory,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
) -> list:
    """Bind the canonical ordered catalog to Claude Agent SDK tools."""
    return [
        bind_sdk_capability(spec)
        for spec in create_builtin_capabilities(
            mm_client,
            memory,
            context_provider=context_provider,
        )
    ]
