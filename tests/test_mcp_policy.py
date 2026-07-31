"""Legacy Runtime 的外部 MCP 能力必须显式授权。"""

from unittest.mock import patch

import pytest

from mmag.mcp_bridge import MCPClientBridge
from mmag.tools import ToolRegistry


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
