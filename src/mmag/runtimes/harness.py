"""Native Deep Agents/LangChain harness configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents.middleware import FilesystemPermission
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware

if TYPE_CHECKING:
    from langchain.agents.middleware.types import AgentMiddleware

    from .base import RunRequest


def build_run_limit_middleware(
    request: RunRequest,
) -> tuple[AgentMiddleware[Any, Any, Any], ...]:
    """Enforce Package call budgets in native graph state across resume boundaries."""
    return (
        ModelCallLimitMiddleware(
            run_limit=request.max_rounds,
            thread_limit=request.max_rounds,
            exit_behavior="error",
        ),
        ToolCallLimitMiddleware(
            run_limit=request.max_tool_calls,
            thread_limit=request.max_tool_calls,
            exit_behavior="error",
        ),
    )


def build_state_filesystem_permissions() -> list[FilesystemPermission]:
    """Restrict native file tools to trusted Skills and ephemeral working state."""
    return [
        FilesystemPermission(
            operations=["read"],
            paths=["/skills/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/skills/**"],
            mode="deny",
        ),
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/workspace/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/**"],
            mode="deny",
        ),
    ]
