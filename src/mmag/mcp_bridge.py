"""Governed MCP configuration, discovery, and Capability registration."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .capabilities import (
    CapabilityExecutor,
    CapabilitySpec,
    bind_langgraph_capability,
    create_mcp_capability,
)
from .logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .capabilities import CapabilityRegistry

log = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "mcp-config.schema.json"
_MCP_RUNTIME_ENVIRONMENT = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)


class MCPConfigError(ValueError):
    """Raised when the unified MCP configuration is invalid."""


@dataclass(frozen=True)
class MCPServerConfig:
    """Immutable server declaration from the startup configuration snapshot."""

    name: str
    transport: str
    enabled: bool
    tools: tuple[str, ...]
    raw_config: Mapping[str, Any]

    def capability_name(self, tool_name: str) -> str:
        return f"mcp_{self.name}_{tool_name}"


@dataclass(frozen=True)
class MCPConfigSnapshot:
    """Validated MCP configuration captured once for one application process."""

    path: Path
    version: int
    sha256: str
    servers: tuple[MCPServerConfig, ...]

    @classmethod
    def empty(cls, path: str | Path = ".mcp.json") -> MCPConfigSnapshot:
        return cls(path=Path(path), version=1, sha256="", servers=())

    @property
    def enabled_servers(self) -> tuple[MCPServerConfig, ...]:
        return tuple(server for server in self.servers if server.enabled)

    @property
    def capability_names(self) -> tuple[str, ...]:
        return tuple(
            server.capability_name(tool)
            for server in self.enabled_servers
            for tool in server.tools
        )

    def get(self, name: str) -> MCPServerConfig | None:
        return next((server for server in self.servers if server.name == name), None)


class _MCPConn(NamedTuple):
    transport: Any
    session: Any


def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _config_error(path: Path, error: ValidationError) -> MCPConfigError:
    location = ".".join(str(part) for part in error.absolute_path) or "root"
    return MCPConfigError(f"invalid MCP config {path} at {location}: {error.message}")


def load_mcp_config(config_path: str | Path) -> MCPConfigSnapshot:
    """Load one strict platform MCP snapshot; invalid configuration fails startup."""
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        log.info("MCP 配置不存在，外部能力保持关闭: %s", path)
        return MCPConfigSnapshot.empty(path)

    try:
        encoded = path.read_bytes()
        raw = json.loads(encoded)
    except (OSError, json.JSONDecodeError) as error:
        raise MCPConfigError(f"cannot load MCP config {path}: {error}") from error

    try:
        Draft202012Validator(_schema()).validate(raw)
    except ValidationError as error:
        raise _config_error(path, error) from error

    servers = tuple(
        MCPServerConfig(
            name=name,
            transport=config["type"],
            enabled=config["enabled"],
            tools=tuple(config["tools"]),
            raw_config=MappingProxyType(dict(config)),
        )
        for name, config in raw["mcpServers"].items()
    )
    snapshot = MCPConfigSnapshot(
        path=path,
        version=raw["version"],
        sha256=hashlib.sha256(encoded).hexdigest(),
        servers=servers,
    )
    log.info(
        "MCP 配置已加载: path=%s version=%d hash=%s servers=%d enabled=%d tools=%d",
        path,
        snapshot.version,
        snapshot.sha256[:16],
        len(snapshot.servers),
        len(snapshot.enabled_servers),
        len(snapshot.capability_names),
    )
    return snapshot


def _resolve_env_vars(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Resolve explicit ${NAME} references without inheriting arbitrary secrets."""
    variables = os.environ if env is None else env
    if isinstance(value, str):

        def replacer(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                raise MCPConfigError(f"MCP environment variable {name!r} is not set")
            return variables[name]

        return re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", replacer, value)
    if isinstance(value, list):
        return [_resolve_env_vars(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_env_vars(item, variables) for key, item in value.items()}
    return value


def _stdio_environment(configured: Any) -> dict[str, str]:
    """Build a minimal child environment plus values explicitly granted by config."""
    inherited = {
        name: value for name, value in os.environ.items() if name in _MCP_RUNTIME_ENVIRONMENT
    }
    if isinstance(configured, dict):
        inherited.update(
            {
                name: value
                for name, value in configured.items()
                if isinstance(name, str) and isinstance(value, str)
            }
        )
    return inherited


class MCPClientBridge:
    """Connect enabled servers and project exact configured tools as Capabilities."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        config: MCPConfigSnapshot,
        executor: CapabilityExecutor,
    ) -> None:
        self.registry = registry
        self.config = config
        self.executor = executor
        self._sessions: dict[str, _MCPConn] = {}
        self._capabilities: dict[str, CapabilitySpec] = {}

    def is_tool_allowed(self, server_name: str, tool_name: str) -> bool:
        server = self.config.get(server_name)
        return bool(server and server.enabled and tool_name in server.tools)

    def get_capabilities(self) -> tuple[CapabilitySpec, ...]:
        return tuple(self._capabilities.values())

    async def load_and_connect(self) -> int:
        if not self.config.enabled_servers:
            log.info("MCP 配置中没有启用的 Server，跳过连接")
            return 0

        success_count = 0
        for server in self.config.enabled_servers:
            try:
                tool_count = await self._connect_one(server)
                if tool_count:
                    success_count += 1
                    log.info("MCP Server '%s' 已就绪: %d 个工具", server.name, tool_count)
            except Exception as error:
                log.warning(
                    "MCP Server '%s' 连接失败: %s [%s]",
                    server.name,
                    error,
                    type(error).__name__,
                )
        log.info(
            "MCP Bridge 就绪: %d/%d 个启用 Server 在线",
            success_count,
            len(self.config.enabled_servers),
        )
        return success_count

    async def _connect_one(self, server: MCPServerConfig) -> int:
        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.client.streamable_http import streamablehttp_client

        config = _resolve_env_vars(dict(server.raw_config))
        transport: Any | None = None
        session: Any | None = None
        try:
            if server.transport == "stdio":
                params = StdioServerParameters(
                    command=config["command"],
                    args=list(config.get("args", [])),
                    env=_stdio_environment(config.get("env", {})),
                )
                transport = stdio_client(params)
            elif server.transport == "sse":
                transport = sse_client(config["url"], headers=config.get("headers", {}))
            else:
                transport = streamablehttp_client(
                    config["url"], headers=config.get("headers", {})
                )

            streams = await transport.__aenter__()
            session = ClientSession(*streams[:2])
            await session.__aenter__()
            await session.initialize()
            self._sessions[server.name] = _MCPConn(transport=transport, session=session)
            result = await session.list_tools()
            registered = self._register_discovered_tools(server, session, result.tools)
            if not registered:
                await self._close_conn(server.name, transport, session)
                self._sessions.pop(server.name, None)
            return registered
        except BaseException:
            await self._close_conn(server.name, transport, session)
            self._sessions.pop(server.name, None)
            raise

    @staticmethod
    async def _close_conn(name: str, transport: Any, session: Any) -> None:
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except Exception as error:
                log.warning("MCP Server '%s' session 关闭异常: %s", name, error)
        if transport is not None:
            try:
                await transport.__aexit__(None, None, None)
            except Exception as error:
                log.warning("MCP Server '%s' transport 关闭异常: %s", name, error)

    def _register_discovered_tools(
        self,
        server: MCPServerConfig,
        session: Any,
        tools: list[Any],
    ) -> int:
        registered = 0
        discovered = {tool.name for tool in tools}
        missing = set(server.tools) - discovered
        if missing:
            log.warning(
                "MCP Server '%s' 未暴露配置中的工具: %s",
                server.name,
                ", ".join(sorted(missing)),
            )
        for tool in tools:
            capability_name = server.capability_name(tool.name)
            if tool.name not in server.tools:
                log.debug("MCP 工具未启用，跳过注册: %s", capability_name)
                continue
            spec = create_mcp_capability(server.name, tool, session)
            self._capabilities[spec.name] = spec
            self.registry.register(bind_langgraph_capability(spec, executor=self.executor))
            registered += 1
        return registered

    async def close_all(self) -> None:
        # AnyIO transports install cancel scopes in the task that enters them.
        # Multiple long-lived connections must therefore leave in strict LIFO
        # order or shutdown fails with a cancel-scope RuntimeError.
        for name, conn in reversed(list(self._sessions.items())):
            await self._close_conn(name, conn.transport, conn.session)
            removed = self.registry.unregister_prefix(f"mcp_{name}_")
            for capability_name in tuple(self._capabilities):
                if capability_name.startswith(f"mcp_{name}_"):
                    del self._capabilities[capability_name]
            if removed:
                log.debug("MCP Server '%s': 已注销 %d 个工具", name, removed)
        self._sessions.clear()
