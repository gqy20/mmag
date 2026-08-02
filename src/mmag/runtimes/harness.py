"""Native Deep Agents/LangChain harness configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from deepagents.middleware import FilesystemPermission
from langchain.agents.middleware import (
    InterruptOnConfig,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    wrap_model_call,
)

from ..capabilities import AuthorizationDecision

if TYPE_CHECKING:
    from langchain.agents.middleware.types import AgentMiddleware

    from ..capabilities import CapabilityRegistry
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


def build_tool_visibility_middleware(
    request: RunRequest,
    *,
    execute_enabled: bool | None = None,
) -> tuple[AgentMiddleware[Any, Any, Any], ...]:
    """Hide native execute unless the governed Run explicitly receives it."""
    allowed = request.metadata.get("capabilities", "").split(",")
    is_enabled = "workspace.execute" in allowed if execute_enabled is None else execute_enabled
    if is_enabled:
        return ()

    @wrap_model_call(name="MMAGToolVisibilityMiddleware")
    async def hide_execute(model_request, handler):
        tools = [tool for tool in model_request.tools if _tool_name(tool) != "execute"]
        return await handler(model_request.override(tools=tools))

    return (hide_execute,)


def _tool_name(tool: Any) -> str:
    if isinstance(tool, Mapping):
        return str(tool.get("name", ""))
    return str(getattr(tool, "name", ""))


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


def build_workspace_interrupt_rules(
    capabilities: tuple[Mapping[str, Any], ...],
    registry: CapabilityRegistry,
) -> dict[str, InterruptOnConfig]:
    """Map native workspace tools to canonical Capability approval decisions."""
    available = {str(schema["name"]) for schema in capabilities}
    rules: dict[str, InterruptOnConfig] = {}
    for tool_name, capability_name in {
        "write_file": "workspace.write",
        "edit_file": "workspace.write",
        "execute": "workspace.execute",
    }.items():
        if capability_name not in available:
            continue

        def requires_approval(tool_request, name=capability_name):
            arguments = dict(tool_request.tool_call.get("args") or {})
            if name == "workspace.write":
                arguments = {
                    "operation": tool_request.tool_call.get("name", "write"),
                    "path": arguments.get("file_path", ""),
                }
            authorization = registry.authorization(name, arguments)
            return bool(
                authorization
                and authorization.decision is AuthorizationDecision.REQUIRE_APPROVAL
            )

        rules[tool_name] = InterruptOnConfig(
            allowed_decisions=["approve", "reject"],
            when=requires_approval,
        )
    return rules
