"""外部 MCP 必须与内置能力共享 Catalog、Policy 和 Runtime 可见性。"""

import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mmag.capabilities import (
    CapabilityAuthorization,
    CapabilityEffect,
    CapabilityExecutor,
    CapabilityRegistry,
    CapabilityStatus,
    SourcePolicy,
)
from mmag.mcp_bridge import (
    MCPClientBridge,
    MCPConfigError,
    MCPConfigSnapshot,
    MCPServerConfig,
    _stdio_environment,
    load_mcp_config,
)


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


def _mcp_config(
    *,
    server: str = "docs",
    tools: tuple[str, ...] = ("search",),
    enabled: bool = True,
) -> MCPConfigSnapshot:
    declaration = MCPServerConfig(
        name=server,
        transport="stdio",
        enabled=enabled,
        tools=tools,
        raw_config=MappingProxyType(
            {
                "enabled": enabled,
                "type": "stdio",
                "command": "mcp-server",
                "tools": list(tools),
            }
        ),
    )
    return MCPConfigSnapshot(
        path=Path(".mcp.json"),
        version=1,
        sha256="test",
        servers=(declaration,),
    )


def test_external_mcp_tools_are_denied_by_default():
    bridge = MCPClientBridge(CapabilityRegistry(), config=MCPConfigSnapshot.empty())

    assert bridge.is_tool_allowed("docs", "search") is False


def test_external_mcp_allowlist_matches_exact_tool_name():
    bridge = MCPClientBridge(
        CapabilityRegistry(),
        config=_mcp_config(),
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
async def test_disabled_servers_skip_external_mcp_connections():
    bridge = MCPClientBridge(
        CapabilityRegistry(),
        config=_mcp_config(enabled=False),
    )

    connected = await bridge.load_and_connect()
    assert connected == 0


def test_unified_config_loads_exact_enabled_capabilities(tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {
                        "enabled": True,
                        "type": "stdio",
                        "command": "docs-mcp",
                        "tools": ["search", "read"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_mcp_config(path)

    assert config.capability_names == ("mcp_docs_search", "mcp_docs_read")
    assert len(config.sha256) == 64


def test_unified_config_rejects_enabled_server_without_tools(tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {
                        "enabled": True,
                        "type": "stdio",
                        "command": "docs-mcp",
                        "tools": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MCPConfigError, match="should be non-empty"):
        load_mcp_config(path)


@pytest.mark.asyncio
async def test_discovered_mcp_tool_uses_one_governed_capability_binding():
    registry = CapabilityRegistry()
    authorizer = MagicMock()
    authorizer.authorize.return_value = CapabilityAuthorization.allow()
    bridge = MCPClientBridge(
        registry,
        config=_mcp_config(),
        executor=CapabilityExecutor(authorizer),
    )
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=_mcp_result(
                '{"results":[{"url":"https://example.com","title":"Example"}]}'
            )
        )
    )

    server = bridge.config.get("docs")
    assert server is not None
    registered = bridge._register_discovered_tools(server, session, [_mcp_tool("search")])
    spec = bridge.get_capabilities()[0]
    result = await registry.execute(spec.name, {"query": "architecture"})

    assert registered == 1
    assert spec.name == "mcp_docs_search"
    assert spec.effect is CapabilityEffect.READ
    assert spec.permission == "mcp:docs:search:invoke"
    assert spec.source_policy is SourcePolicy.AUTO
    assert result.data["_sources"]
    session.call_tool.assert_awaited_once()
    authorizer.authorize.assert_called_once()


@pytest.mark.asyncio
async def test_mcp_policy_denial_stops_the_canonical_binding():
    registry = CapabilityRegistry()
    authorizer = MagicMock()
    authorizer.authorize.return_value = CapabilityAuthorization.deny("disabled")
    bridge = MCPClientBridge(
        registry,
        config=_mcp_config(server="ops", tools=("deploy",)),
        executor=CapabilityExecutor(authorizer),
    )
    session = SimpleNamespace(call_tool=AsyncMock())

    server = bridge.config.get("ops")
    assert server is not None
    bridge._register_discovered_tools(server, session, [_mcp_tool("deploy", read_only=None)])
    spec = bridge.get_capabilities()[0]
    result = await registry.execute(spec.name, {"query": "release"})

    assert spec.effect is CapabilityEffect.WRITE
    assert result.status is CapabilityStatus.FORBIDDEN
    session.call_tool.assert_not_awaited()


def test_only_allowlisted_discovered_tools_are_visible():
    registry = CapabilityRegistry()
    bridge = MCPClientBridge(
        registry,
        config=_mcp_config(),
    )

    server = bridge.config.get("docs")
    assert server is not None
    registered = bridge._register_discovered_tools(
        server,
        SimpleNamespace(call_tool=AsyncMock()),
        [_mcp_tool("search"), _mcp_tool("delete", read_only=False)],
    )

    assert registered == 1
    assert [tool.name for tool in registry.get_all()] == ["mcp_docs_search"]
