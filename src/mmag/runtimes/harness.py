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
    from langchain_core.tools import StructuredTool

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


def build_tool_discovery(
    tool_schemas: tuple[Mapping[str, Any], ...],
) -> tuple[list[StructuredTool], tuple[AgentMiddleware[Any, Any, Any], ...], str]:
    """Progressive tool disclosure via system prompt catalog.

    Injects a tool catalog (name + description) into system prompt so the
    model knows all available capabilities upfront with clear guidance on
    which tool to use. All tool schemas remain in the tools= list for
    reliability with proxies that may not support dynamic tool overrides.
    """
    meta_names: set[str] = set()
    catalog: dict[str, Mapping[str, Any]] = {
        str(s["name"]): s for s in tool_schemas if str(s["name"]) not in meta_names
    }

    catalog_lines = [
        f"- {name}: {str(schema.get('description') or name)}"
        for name, schema in catalog.items()
    ]
    catalog_prompt = (
        "\n\n## 可用工具目录\n"
        "以下是你的全部工具。当用户的请求匹配某个工具时，直接调用它：\n"
        + "\n".join(catalog_lines)
    )

    return [], (), catalog_prompt
