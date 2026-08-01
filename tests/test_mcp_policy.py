"""外部 MCP 必须与内置能力共享 Catalog、Policy 和 Runtime 可见性。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mmag.capabilities import (
    CapabilityAuthorization,
    CapabilityEffect,
    CapabilityExecutor,
    CapabilityRegistry,
    CapabilityStatus,
    SourcePolicy,
)
from mmag.mcp_bridge import MCPClientBridge, _stdio_environment


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
    bridge = MCPClientBridge(CapabilityRegistry())

    assert bridge.is_tool_allowed("docs", "search") is False


def test_external_mcp_allowlist_matches_exact_tool_name():
    bridge = MCPClientBridge(
        CapabilityRegistry(),
        allowed_tools={"mcp_docs_search"},
    )

    assert bridge.is_tool_allowed("docs", "search") is True
    assert bridge.is_tool_allowed("docs", "delete") is False


def test_stdio_environment_only_inherits_runtime_basics_and_explicit_server_values(
    monkeypatch,
):
    monkeypatch.setenv("PATH", "/runtime/bin")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    monkeypatch.setenv("MM_TOKEN", "mattermost-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "model-secret")

    environment = _stdio_environment({"DOCS_TOKEN": "explicit-secret"})

    assert environment["PATH"] == "/runtime/bin"
    assert environment["LANG"] == "zh_CN.UTF-8"
    assert environment["DOCS_TOKEN"] == "explicit-secret"
    assert "MM_TOKEN" not in environment
    assert "ANTHROPIC_API_KEY" not in environment


@pytest.mark.asyncio
async def test_empty_allowlist_skips_external_mcp_connections():
    bridge = MCPClientBridge(CapabilityRegistry())

    with patch("mmag.mcp_bridge.read_mcp_config") as read_config:
        connected = await bridge.load_and_connect()

    assert connected == 0
    read_config.assert_not_called()


@pytest.mark.asyncio
async def test_discovered_mcp_tool_uses_one_governed_capability_binding():
    registry = CapabilityRegistry()
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
    result = await registry.execute(spec.name, {"query": "architecture"})

    assert registered == 1
    assert spec.name == "mcp_docs_search"
    assert spec.effect is CapabilityEffect.READ
    assert spec.permission == "mcp:docs:search:invoke"
    assert spec.source_policy is SourcePolicy.AUTO
    assert '"_sources"' in result
    session.call_tool.assert_awaited_once()
    authorizer.authorize.assert_called_once()


@pytest.mark.asyncio
async def test_mcp_policy_denial_stops_the_canonical_binding():
    registry = CapabilityRegistry()
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
    result = await registry.execute(spec.name, {"query": "release"})

    assert spec.effect is CapabilityEffect.WRITE
    assert f'"code": "{CapabilityStatus.FORBIDDEN}"' in result
    session.call_tool.assert_not_awaited()


def test_only_allowlisted_discovered_tools_are_visible():
    registry = CapabilityRegistry()
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
