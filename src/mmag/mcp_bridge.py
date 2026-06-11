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
from typing import TYPE_CHECKING, Any

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
    required_env_vars: list[str] = field(default_factory=list)
    raw_config: dict[str, Any] = field(default_factory=dict)


def _collect_template_vars(value: Any, out: set[str]) -> None:
    """递归收集 ${VAR_NAME} 模板变量"""
    if isinstance(value, str):
        for m in re.finditer(r"\$\{([A-Z0-9_]+)\}", value):
            var_name = m.group(1)
            if var_name:
                out.add(var_name)
    elif isinstance(value, list):
        for item in value:
            _collect_template_vars(item, out)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_template_vars(v, out)


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

        server_type = (
            str(cfg.get("type", "")).strip()
            or ("stdio" if "command" in cfg else "http")
        )
        required_vars: set[str] = set()
        _collect_template_vars(cfg, required_vars)

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
                required_env_vars=sorted(required_vars),
                raw_config=cfg,
            )
        )

    log.info(
        "MCP 配置已加载: %s (%d 个 Server)",
        path.relative_to(_PROJECT_ROOT),
        len(items),
    )
    for item in items:
        log.debug(
            "  - [%s] type=%s endpoint=%s env=%s",
            item.name,
            item.type,
            item.endpoint[:80],
            item.required_vars or "-",
        )
    return items


class MCPClientBridge:
    """MCP 客户端桥接器 — 连接外部 MCP Server，注册工具到 ToolRegistry

    用法::

        bridge = MCPClientBridge(registry)
        await bridge.load_and_connect()   # 读 .mcp.json + 连接所有 Server
        # 之后 registry 中就有了 mcp_xxx_yyy 形式的工具，LLM 可直接调用
    """

    def __init__(self, registry: ToolRegistry, config_path: str | None = None):
        self.registry = registry
        self.config_path = config_path
        self._sessions: dict[str, Any] = {}  # name → ClientSession
        self._connected = False

    @property
    def connected_servers(self) -> list[str]:
        """已连接的 Server 名称列表"""
        return list(self._sessions.keys())

    async def load_and_connect(self) -> int:
        """读取 .mcp.json 并连接所有配置的 Server

        Returns:
            成功连接并注册工具的 Server 数量
        """
        items = read_mcp_config(self.config_path)
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

        self._connected = success_count > 0
        if success_count > 0:
            log.info("MCP Bridge 就绪: %d/%d 个 Server 在线", success_count, len(items))
        return success_count

    async def _connect_one(self, item: McpConfigItem) -> int:
        """连接单个 MCP Server 并注册其所有工具

        Returns:
            注册的工具数量
        """
        from mcp.client.sse import sse_client
        from mcp.client.stdio import StdioServerParameters, stdio_client

        # ── 建立连接 ──
        session: Any | None = None

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
            client = stdio_client(params)
            session = await client.__aenter__()

        elif item.type in ("http", "streamable-http"):
            url = item.raw_config.get("url", "")
            headers = item.raw_config.get("headers", {})
            # 过滤掉 Authorization 等 header 中可能未解析的 ${VAR}
            resolved_headers = {
                k: v for k, v in headers.items() if isinstance(k, str) and isinstance(v, str)
            }
            client = sse_client(url, headers=resolved_headers)
            session = await client.__aenter__()

        else:
            log.warning("MCP Server '%s': 不支持的传输类型 '%s'", item.name, item.type)
            return 0

        self._sessions[item.name] = session

        # ── 拉取工具列表 ──
        try:
            result = await session.list_tools()
            tools = result.tools
        except Exception as e:
            log.warning("MCP Server '%s' 获取工具列表失败: %s", item.name, e)
            await session.__aexit__(None, None, None)
            del self._sessions[item.name]
            return 0

        # ── 注册到 ToolRegistry ──
        registered = 0
        for tool in tools:
            tool_name = f"mcp_{item.name}_{tool.name}"
            input_schema = tool.inputSchema or {}

            # 包装 handler: 将调用转发到远程 MCP Server
            handler = self._make_handler(session, tool.name, item.name)

            from .tools import Tool

            self.registry.register(
                Tool(
                    name=tool_name,
                    description=tool.description or f"[MCP:{item.name}] {tool.name}",
                    input_schema=input_schema,
                    handler=handler,
                )
            )
            registered += 1

        return registered

    @staticmethod
    def _make_handler(session: Any, tool_name: str, server_name: str):
        """创建闭包 handler，将 ToolRegistry 调用转发到 MCP Server"""

        async def handler(**kwargs: Any) -> str:
            import json as _json

            try:
                result = await session.call_tool(tool_name, arguments=kwargs)
                # 提取文本内容
                texts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        texts.append(content.text)
                    elif isinstance(content, str):
                        texts.append(content)
                    elif hasattr(content, "__dict__"):
                        texts.append(str(content.__dict__))
                    else:
                        texts.append(str(content))

                output = "\n".join(texts)
                log.debug(
                    "[MCP:%s.%s] → %d 字符",
                    server_name,
                    tool_name,
                    len(output),
                )
                return output

            except Exception as e:
                err_msg = f"MCP 工具调用错误 [{server_name}/{tool_name}]: {type(e).__name__}: {e}"
                log.error(err_msg)
                return _json.dumps({"error": err_msg}, ensure_ascii=False)

        return handler

    async def close_all(self):
        """关闭所有 MCP 连接"""
        for name, session in list(self._sessions.items()):
            try:
                if hasattr(session, "__aexit__"):
                    await session.__aexit__(None, None, None)
                elif hasattr(session, "close"):
                    await session.close()
                log.debug("MCP Server '%s' 已断开", name)
            except Exception as e:
                log.warning("MCP Server '%s' 断开时异常: %s", name, e)
        self._sessions.clear()
        self._connected = False
