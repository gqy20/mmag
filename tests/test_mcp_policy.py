"""外部 MCP 必须与内置能力共享 Catalog、Policy 和 Runtime 可见性。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mmag.capabilities import (
    CapabilityAuthorization,
    CapabilityEffect,
    CapabilityExecutor,
    CapabilityStatus,
    SourcePolicy,
)
from mmag.mcp_bridge import MCPClientBridge
from mmag.tools import ToolRegistry


def _mcp_tool(name: str, *, read_only: bool | None = True):
    annotations = None if read_only is None else SimpleNamespace(readOnlyHint=read_only)
    return SimpleNamespace(
        name=name,
        description=f"{name} docs",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "search query",
                    "default": "",
                }
            },
            "required": [],
        },
        annotations=annotations,
    )


def _mcp_result(payload: str):
    return SimpleNamespace(
        content=[SimpleNamespace(text=payload)],
        isError=False,
    )


def test_external_mcp_tools_are_denied_by_default():
    bridge = MCPClientBridge(ToolRegistry())

    assert bridge.is_tool_allowed("docs", "search") is False


def test_external_mcp_allowlist_matches_exact_tool_name():
    bridge = MCPClientBridge(
        ToolRegistry(),
        allowed_tools={"mcp_docs_search"},
    )

    assert bridge.is_tool_allowed("docs", "search") is True
    assert bridge.is_tool_allowed("docs", "delete") is False


@pytest.mark.asyncio
async def test_empty_allowlist_skips_external_mcp_connections():
    bridge = MCPClientBridge(ToolRegistry())

    with patch("mmag.mcp_bridge.read_mcp_config") as read_config:
        connected = await bridge.load_and_connect()

    assert connected == 0
    read_config.assert_not_called()


@pytest.mark.asyncio
async def test_discovered_mcp_tool_uses_one_spec_for_both_runtime_bindings():
    registry = ToolRegistry()
    authorizer = MagicMock()
    authorizer.authorize.return_value = CapabilityAuthorization.allow()
    bridge = MCPClientBridge(
        registry,
        allowed_tools={"mcp_docs_search"},
        executor=CapabilityExecutor(authorizer),
    )
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=_mcp_result(
                '{"results":[{"url":"https://example.com","title":"Example"}]}'
            )
        )
    )

    registered = bridge._register_discovered_tools("docs", session, [_mcp_tool("search")])
    spec = bridge.get_capabilities()[0]
    sdk_tool = bridge.get_sdk_bindings()[0]
    legacy_result = await registry.execute(spec.name, {"query": "architecture"})
    sdk_result = await sdk_tool.handler({"query": "architecture"})

    assert registered == 1
    assert spec.name == sdk_tool.name == "mcp_docs_search"
    assert spec.effect is CapabilityEffect.READ
    assert spec.permission == "mcp:docs:search:invoke"
    assert spec.source_policy is SourcePolicy.AUTO
    assert sdk_tool.input_schema == dict(spec.input_schema)
    assert '"_sources"' in legacy_result
    assert '"_sources"' in sdk_result["content"][0]["text"]
    assert session.call_tool.await_count == 2
    assert authorizer.authorize.call_count == 2


@pytest.mark.asyncio
async def test_mcp_policy_denial_is_identical_and_stops_both_bindings():
    registry = ToolRegistry()
    authorizer = MagicMock()
    authorizer.authorize.return_value = CapabilityAuthorization.deny("disabled")
    bridge = MCPClientBridge(
        registry,
        allowed_tools={"mcp_ops_deploy"},
        executor=CapabilityExecutor(authorizer),
    )
    session = SimpleNamespace(call_tool=AsyncMock())

    bridge._register_discovered_tools("ops", session, [_mcp_tool("deploy", read_only=None)])
    spec = bridge.get_capabilities()[0]
    legacy_result = await registry.execute(spec.name, {"query": "release"})
    sdk_result = await bridge.get_sdk_bindings()[0].handler({"query": "release"})

    assert spec.effect is CapabilityEffect.WRITE
    assert f'"code": "{CapabilityStatus.FORBIDDEN}"' in legacy_result
    assert f'"code": "{CapabilityStatus.FORBIDDEN}"' in sdk_result["content"][0]["text"]
    session.call_tool.assert_not_awaited()


def test_only_allowlisted_discovered_tools_are_visible_to_both_runtimes():
    registry = ToolRegistry()
    bridge = MCPClientBridge(
        registry,
        allowed_tools={"mcp_docs_search"},
    )

    registered = bridge._register_discovered_tools(
        "docs",
        SimpleNamespace(call_tool=AsyncMock()),
        [_mcp_tool("search"), _mcp_tool("delete", read_only=False)],
    )

    assert registered == 1
    assert [tool.name for tool in registry.get_all()] == ["mcp_docs_search"]
    assert [tool.name for tool in bridge.get_sdk_bindings()] == ["mcp_docs_search"]
