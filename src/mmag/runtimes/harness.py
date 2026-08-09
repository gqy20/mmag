"""Native Deep Agents/LangChain harness configuration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from deepagents.middleware import FilesystemPermission
from langchain.agents.middleware import (
    InterruptOnConfig,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    wrap_model_call,
)
from langchain_core.tools import StructuredTool

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


def build_tool_discovery(
    tool_schemas: tuple[Mapping[str, Any], ...],
) -> tuple[list[StructuredTool], tuple[AgentMiddleware[Any, Any, Any], ...]]:
    """Progressive tool disclosure via search_tools + get_tool_details meta-tools.

    Returns meta-tools to add to the tool list and a middleware that hides
    capability tools until the model discovers them via get_tool_details.
    Native Deep Agents tools (read_file, ls, etc.) are always visible.
    """
    meta_names = {"search_tools", "get_tool_details"}
    cap_names = {str(s["name"]) for s in tool_schemas}
    discovered: set[str] = set()

    catalog: dict[str, Mapping[str, Any]] = {
        str(s["name"]): s for s in tool_schemas if str(s["name"]) not in meta_names
    }

    async def search_tools(query: str) -> str:
        keywords = [k for k in query.lower().replace(",", " ").split() if k]
        results: list[dict[str, str]] = []
        for name, schema in catalog.items():
            text = f"{name} {schema.get('description', '')}".lower()
            score = sum(1 for kw in keywords if kw in text)
            if score > 0:
                results.append({"name": name, "description": str(schema.get("description") or name)})
        if not results:
            results = [
                {"name": name, "description": str(schema.get("description") or name)}
                for name, schema in catalog.items()
            ]
        return json.dumps({"count": len(results), "tools": results}, ensure_ascii=False)

    async def get_tool_details(name: str) -> str:
        schema = catalog.get(name)
        if schema is None:
            return json.dumps({"error": f"工具 {name} 不存在"}, ensure_ascii=False)
        discovered.add(name)
        return json.dumps(
            {
                "name": name,
                "description": str(schema.get("description") or name),
                "input_schema": schema.get("input_schema") or {"type": "object"},
            },
            ensure_ascii=False,
        )

    search_tool = StructuredTool.from_function(
        coroutine=search_tools,
        name="search_tools",
        description="搜索可用工具。输入关键词（如'创建任务'、'搜索消息'），返回匹配的工具名称和简介。",
        args_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        },
    )
    details_tool = StructuredTool.from_function(
        coroutine=get_tool_details,
        name="get_tool_details",
        description="获取指定工具的完整参数定义。调用后该工具将被解锁供直接使用。",
        args_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "工具名称"}},
            "required": ["name"],
        },
    )

    @wrap_model_call(name="MMAGToolDiscoveryMiddleware")
    async def filter_tools(model_request, handler):
        visible = [
            tool
            for tool in model_request.tools
            if _tool_name(tool) not in cap_names
            or _tool_name(tool) in meta_names
            or _tool_name(tool) in discovered
        ]
        return await handler(model_request.override(tools=visible))

    return [search_tool, details_tool], (filter_tools,)
