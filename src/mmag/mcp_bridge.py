"""
MCP Client Bridge — 读取 .mcp.json，连接外部 MCP Server，注入工具到 ToolRegistry

和 Claude Code 的 .mcp.json 格式完全兼容，可直接复用 cc_plugins 等项目的配置。

支持的传输方式:
  - stdio: 本地进程 (npx / uvx / python)
  - http / streamable-http: 远程服务 (SSE / Streamable HTTP)

配置文件位置（按优先级）:
  1. .env 中 MCP_CONFIG_PATH 指定的路径
  2. 项目根目录的 .mcp.json（CC 标准位置）
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from .capabilities import (
    CapabilityExecutor,
    CapabilitySpec,
    bind_legacy_capability,
    bind_sdk_capability,
    create_mcp_capability,
)
from .logger import get_logger

if TYPE_CHECKING:
    from .tools import ToolRegistry

log = get_logger(__name__)

# 项目根目录（src/mmag 上两级）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class McpConfigItem:
    """解析后的单个 MCP Server 配置"""

    name: str
    type: str  # "stdio" | "http" | "streamable-http"
    endpoint: str  # command+args 或 url
    raw_config: dict[str, Any] = field(default_factory=dict)


class _McpConn(NamedTuple):
    """一对 MCP 连接资源 — close_all / 异常清理都要按反向关两层

    transport: stdio_client 或 sse_client 上下文 (管子进程 / HTTP 连接)
    session:   ClientSession (管 MCP 协议握手 + list_tools / call_tool)
    """

    transport: Any
    session: Any


def _resolve_env_vars(value: Any, env: dict[str, str] | None = None) -> Any:
    """将 ${VAR_NAME} 替换为环境变量值"""
    env = env or dict(os.environ)
    if isinstance(value, str):

        def replacer(m: re.Match) -> str:
            var_name = m.group(1)
            return env.get(var_name, m.group(0))

        return re.sub(r"\$\{([A-Z0-9_]+)\}", replacer, value)
    elif isinstance(value, list):
        return [_resolve_env_vars(v, env) for v in value]
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v, env) for k, v in value.items()}
    return value


def read_mcp_config(config_path: str | Path | None = None) -> list[McpConfigItem]:
    """读取并解析 .mcp.json 配置文件

    Args:
        config_path: 配置文件路径，None 则自动查找项目根目录

    Returns:
        解析后的 Server 配置列表
    """
    if config_path:
        path = Path(config_path)
    else:
        # 按优先级查找: 环境变量 → 项目根目录 .mcp.json
        env_path = os.getenv("MCP_CONFIG_PATH")
        path = Path(env_path) if env_path else _PROJECT_ROOT / ".mcp.json"

    if not path.exists():
        log.info("未找到 MCP 配置文件: %s（跳过 MCP 加载）", path)
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("MCP 配置文件解析失败 %s: %s", path, e)
        return []

    servers_raw = raw.get("mcpServers", {})
    if not servers_raw or not isinstance(servers_raw, dict):
        log.info("MCP 配置文件无 mcpServers 段: %s", path)
        return []

    items: list[McpConfigItem] = []
    for name, raw_cfg in servers_raw.items():
        if not isinstance(raw_cfg, dict):
            continue
        cfg = _resolve_env_vars(raw_cfg)

        server_type = str(cfg.get("type", "")).strip() or ("stdio" if "command" in cfg else "http")

        endpoint = ""
        if server_type == "stdio":
            cmd = str(cfg.get("command", "")).strip()
            args = cfg.get("args", [])
            if isinstance(args, list):
                args_str = " ".join(str(a) for a in args if isinstance(a, str))
            else:
                args_str = ""
            endpoint = f"{cmd} {args_str}".strip()
        else:
            endpoint = str(cfg.get("url", "")).strip()

        items.append(
            McpConfigItem(
                name=name,
                type=server_type,
                endpoint=endpoint,
                raw_config=cfg,
            )
        )

    log.info(
        "MCP 配置已加载: %s (%d 个 Server)",
        path.relative_to(_PROJECT_ROOT),
        len(items),
    )
    for item in items:
        log.debug("  - [%s] type=%s endpoint=%s", item.name, item.type, item.endpoint[:80])
    return items


class MCPClientBridge:
    """MCP 客户端桥接器 — 连接外部 MCP Server，注册工具到 ToolRegistry

    用法::

        bridge = MCPClientBridge(registry)
        await bridge.load_and_connect()   # 读 .mcp.json + 连接所有 Server
        # 之后 registry 中就有了 mcp_xxx_yyy 形式的工具，LLM 可直接调用
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        allowed_tools: set[str] | frozenset[str] | tuple[str, ...] = (),
        executor: CapabilityExecutor | None = None,
    ):
        self.registry = registry
        self.allowed_tools = frozenset(allowed_tools)
        self.executor = executor or CapabilityExecutor()
        self._sessions: dict[str, _McpConn] = {}  # name → (transport, session)
        self._capabilities: dict[str, CapabilitySpec] = {}

    def is_tool_allowed(self, server_name: str, tool_name: str) -> bool:
        """Return whether an external MCP tool is explicitly enabled."""
        return f"mcp_{server_name}_{tool_name}" in self.allowed_tools

    def get_capabilities(self) -> tuple[CapabilitySpec, ...]:
        """Return the allowlisted external capabilities in discovery order."""
        return tuple(self._capabilities.values())

    def get_sdk_bindings(self) -> list:
        """Generate SDK bindings from the same specs and executor as Legacy."""
        return [
            bind_sdk_capability(spec, executor=self.executor)
            for spec in self._capabilities.values()
        ]

    async def load_and_connect(self) -> int:
        """读取 .mcp.json 并连接所有配置的 Server

        Returns:
            成功连接并注册工具的 Server 数量
        """
        if not self.allowed_tools:
            log.info("外部 MCP 工具未配置白名单，跳过连接")
            return 0

        items = read_mcp_config()
        if not items:
            return 0

        success_count = 0
        for item in items:
            try:
                tool_count = await self._connect_one(item)
                if tool_count > 0:
                    success_count += 1
                    log.info(
                        "MCP Server '%s' 已就绪: %d 个工具",
                        item.name,
                        tool_count,
                    )
            except Exception as e:
                log.warning(
                    "MCP Server '%s' 连接失败: %s [%s]",
                    item.name,
                    e,
                    type(e).__name__,
                )

        if success_count > 0:
            log.info("MCP Bridge 就绪: %d/%d 个 Server 在线", success_count, len(items))
        return success_count

    async def _connect_one(self, item: McpConfigItem) -> int:
        """连接单个 MCP Server 并注册其所有工具

        MCP SDK 的 stdio_client / sse_client 实际是 async context manager,
        yield 的是 (read_stream, write_stream) tuple, 不是 session。
        需要再包一层 ClientSession(read, write) 才能拿到 list_tools / call_tool。

        资源管理: transport 和 session 是两层独立的 context manager,
        任一异常都需要按反向顺序 (session → transport) 关闭, 否则 stdio
        子进程 / HTTP 连接会泄漏。

        Returns:
            注册的工具数量
        """
        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client
        from mcp.client.stdio import StdioServerParameters, stdio_client

        transport: Any | None = None
        session: Any | None = None
        try:
            # 1) 建 transport (stdio_client / sse_client 是 async context manager)
            if item.type == "stdio":
                cfg = item.raw_config
                params = StdioServerParameters(
                    command=cfg.get("command", ""),
                    args=[str(a) for a in cfg.get("args", []) if isinstance(a, str)],
                    env={
                        **os.environ,
                        **{
                            k: v
                            for k, v in cfg.get("env", {}).items()
                            if isinstance(k, str) and isinstance(v, str)
                        },
                    },
                )
                transport = stdio_client(params)
                streams = await transport.__aenter__()

            elif item.type in ("http", "streamable-http"):
                url = item.raw_config.get("url", "")
                headers = item.raw_config.get("headers", {})
                resolved_headers = {
                    k: v for k, v in headers.items() if isinstance(k, str) and isinstance(v, str)
                }
                transport = sse_client(url, headers=resolved_headers)
                streams = await transport.__aenter__()

            else:
                log.warning("MCP Server '%s': 不支持的传输类型 '%s'", item.name, item.type)
                return 0

            # 2) 包 ClientSession 拿到真正的 MCP 协议句柄
            session = ClientSession(*streams)
            await session.__aenter__()
            # 2.5) MCP 协议握手: 必须先 initialize() 才能调 list_tools / call_tool,
            #      否则服务端会报 "Received request before initialization was complete"
            try:
                await session.initialize()
            except Exception as e:
                log.warning("MCP Server '%s' initialize 失败: %s", item.name, e)
                await self._close_conn(item.name, transport, session)
                self._sessions.pop(item.name, None)
                return 0
            self._sessions[item.name] = _McpConn(transport=transport, session=session)

            # 3) 拉取工具列表
            try:
                result = await session.list_tools()
                tools = result.tools
            except Exception as e:
                log.warning("MCP Server '%s' 获取工具列表失败: %s", item.name, e)
                # 失败也要把 session/transport 关掉
                await self._close_conn(item.name, transport, session)
                self._sessions.pop(item.name, None)
                return 0

            # 4) 发现结果先进入 Capability，再派生 Legacy/SDK bindings。
            registered = self._register_discovered_tools(item.name, session, tools)
            if registered == 0:
                await self._close_conn(item.name, transport, session)
                self._sessions.pop(item.name, None)
            return registered

        except BaseException:
            # __aenter__ 成功但 list_tools / register 之前任意步骤失败:
            # 反向关闭两层 (session → transport)
            await self._close_conn(item.name, transport, session)
            self._sessions.pop(item.name, None)
            raise

    @staticmethod
    async def _close_conn(name: str, transport: Any, session: Any) -> None:
        """反向关闭 MCP 连接的两层 (session → transport),吞所有异常

        用于异常路径和 list_tools 失败时的局部清理。
        """
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except Exception as e:
                log.warning("MCP Server '%s' session 关闭异常: %s", name, e)
        if transport is not None:
            try:
                await transport.__aexit__(None, None, None)
            except Exception as e:
                log.warning("MCP Server '%s' transport 关闭异常: %s", name, e)

    def _register_discovered_tools(
        self,
        server_name: str,
        session: Any,
        tools: list[Any],
    ) -> int:
        """Register allowlisted discovery results through the Capability policy path."""
        registered = 0
        for tool in tools:
            capability_name = f"mcp_{server_name}_{tool.name}"
            if not self.is_tool_allowed(server_name, tool.name):
                log.warning("MCP 工具未在白名单中，跳过注册: %s", capability_name)
                continue
            spec = create_mcp_capability(server_name, tool, session)
            self._capabilities[spec.name] = spec
            self.registry.register(
                bind_legacy_capability(spec, executor=self.executor)
            )
            registered += 1
        return registered

    async def close_all(self):
        """关闭所有 MCP 连接，并同步注销注入到 ToolRegistry 的 mcp_* 工具

        避免 session 关闭后 LLM 仍可调用死 session 的工具（会抛连接错误）。

        关闭顺序: session → transport (反向建立顺序)
        """
        for name, conn in list(self._sessions.items()):
            await self._close_conn(name, conn.transport, conn.session)
            # 注销该 Server 注入的全部工具（mcp_<name>_*）
            removed = self.registry.unregister_prefix(f"mcp_{name}_")
            capability_names = [
                capability_name
                for capability_name in self._capabilities
                if capability_name.startswith(f"mcp_{name}_")
            ]
            for capability_name in capability_names:
                del self._capabilities[capability_name]
            if removed:
                log.debug("MCP Server '%s': 已注销 %d 个工具", name, removed)
        self._sessions.clear()
